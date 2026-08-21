"""Cross-platform desktop client (Windows/Linux/macOS) for the Auto File
Processor: a Tkinter GUI wrapping upload_watcher.py (concurrent auto-upload
of a local folder), download_manager.py (concurrent recursive download of
the organized output), and sync_manager.py (continuous two-way
reconciliation -- the "just keep a folder in sync, Nextcloud-style"
feature). Pure Tkinter + stdlib/this package's own deps, so the exact same
source builds into a native binary on all three OSes via PyInstaller (see
client/build/*.sh, client/windows/build-manager.ps1) -- unlike
windows/manager.py, nothing here touches a Windows-only API, so this module
has no platform guard on launch.

Runs in the background: closing the window minimizes to a system tray icon
(pystray) instead of quitting, so an active sync loop or upload watcher
keeps running unattended. Actually quitting is a separate "종료" action from
the tray menu. Combined with autostart.py (launch on login), this is what
makes "always in sync, regardless of who uploads" actually true rather than
only true while someone remembers the window is open.

The sync tab's folder/remote-path/concurrency and its "자동 동기화" checkbox
are persisted to ClientSettings (see config.py) as soon as the loop is
started or the checkbox is toggled. On the next launch, if the checkbox was
on, _maybe_auto_start_sync() resumes the loop by itself -- no button press
required, which combined with tray + autostart is what makes this behave
like an always-on service rather than something the user has to remember to
turn back on.

On Windows only, an extra "Mount (Windows)" tab is shown that reuses the
existing rclone-based WebDAV mount from windows/mount.py -- that mechanism
depends on WinFsp/rclone and isn't something Linux/macOS can share, so it
stays exactly as it already was rather than being ported here.
"""

from __future__ import annotations

import platform
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from file_sorting_client import __version__
from file_sorting_client import autostart
from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.config import ClientSettings
from file_sorting_client.download_manager import download_tree
from file_sorting_client.sync_manager import SyncLoop, force_upload_folder, list_local_files, run_sync
from file_sorting_client.updater import UpdateInfo, apply_update_linux, check_for_update
from file_sorting_client.upload_watcher import UploadWatcher


def is_windows() -> bool:
    return platform.system() == "Windows"


def _tray_icon_image():
    # Generated at runtime instead of shipping an asset file -- keeps the
    # PyInstaller build a single source tree with no binary resources to
    # track. Simple enough that a drawn shape is plenty recognizable at
    # tray-icon size.
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=(99, 102, 241, 255))
    draw.polygon([(20, 34), (32, 20), (44, 34), (36, 34), (36, 46), (28, 46), (28, 34)], fill=(255, 255, 255, 255))
    return img


class FileSortingClientApp:
    # How often a long-running instance (tray-minimized, autostarted at
    # login) re-checks for a new build after the initial startup check.
    # Without this, an app that's never quit -- exactly the normal way to
    # run this -- would never notice a new version until someone thought to
    # restart it by hand.
    _UPDATE_RECHECK_MS = 30 * 60 * 1000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"File Sorting Client v{__version__}")
        self.root.geometry("720x600")

        self.settings = ClientSettings.load()
        self._watcher: Optional[UploadWatcher] = None
        self._sync_loop: Optional[SyncLoop] = None
        self._ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._tray_icon = None
        self._quitting = False

        self._build_settings_frame()
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.upload_tab = ttk.Frame(notebook)
        self.download_tab = ttk.Frame(notebook)
        self.sync_tab = ttk.Frame(notebook)
        notebook.add(self.upload_tab, text="자동 업로드")
        notebook.add(self.download_tab, text="다운로드")
        notebook.add(self.sync_tab, text="동기화")

        self._build_upload_tab()
        self._build_download_tab()
        self._build_sync_tab()

        if is_windows():
            mount_tab = ttk.Frame(notebook)
            notebook.add(mount_tab, text="드라이브 마운트 (Windows)")
            ttk.Label(
                mount_tab,
                text=(
                    "고급 옵션: rclone 기반 WebDAV 드라이브 마운트는\n"
                    "별도 프로그램인 FileSortingManager.exe 로 실행해주세요."
                ),
                justify="center",
            ).pack(expand=True)

        self.root.after(150, self._drain_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._setup_tray()
        self.root.after(1500, self._check_for_updates_async)
        self.root.after(1000, self._maybe_auto_start_sync)

    # -- settings -----------------------------------------------------
    def _build_settings_frame(self) -> None:
        frame = ttk.LabelFrame(self.root, text="서버 설정")
        frame.pack(fill="x", padx=8, pady=(8, 0))

        ttk.Label(frame, text="서버 주소").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.base_url_var = tk.StringVar(value=self.settings.api_base_url)
        ttk.Entry(frame, textvariable=self.base_url_var, width=50).grid(
            row=0, column=1, sticky="we", padx=4, pady=4
        )

        ttk.Label(frame, text="API 토큰").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.token_var = tk.StringVar(value=self.settings.api_token)
        ttk.Entry(frame, textvariable=self.token_var, width=50, show="*").grid(
            row=1, column=1, sticky="we", padx=4, pady=4
        )

        ttk.Button(frame, text="저장", command=self._save_settings).grid(row=0, column=2, rowspan=2, padx=4)

        self.autostart_var = tk.BooleanVar(value=autostart.is_supported() and autostart.is_enabled())
        autostart_check = ttk.Checkbutton(
            frame,
            text="로그인 시 자동 시작 (백그라운드)",
            variable=self.autostart_var,
            command=self._toggle_autostart,
            state="normal" if autostart.is_supported() else "disabled",
        )
        autostart_check.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4))
        if not autostart.is_supported():
            ttk.Label(
                frame, text="(소스에서 직접 실행 중 -- 빌드된 실행 파일에서만 사용 가능)", foreground="#888"
            ).grid(row=2, column=2, sticky="w", padx=4)

        self.update_label = ttk.Label(frame, text="", foreground="#4f46e5")
        self.update_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=4)
        self.update_btn = ttk.Button(frame, text="업데이트", command=self._apply_update)
        # gridded on demand once an update is actually found -- see _show_update_banner

        frame.columnconfigure(1, weight=1)

    def _save_settings(self) -> None:
        self.settings = self.settings.with_overrides(
            api_base_url=self.base_url_var.get().strip(),
            api_token=self.token_var.get().strip(),
        )
        self.settings.save()
        messagebox.showinfo("저장 완료", f"설정을 저장했습니다:\n{self.settings.config_file}")

    def _toggle_autostart(self) -> None:
        try:
            if self.autostart_var.get():
                autostart.enable()
            else:
                autostart.disable()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("자동 시작 설정 실패", str(exc))
            self.autostart_var.set(not self.autostart_var.get())

    def _make_client(self) -> FileSortingApiClient:
        current = self.settings.with_overrides(
            api_base_url=self.base_url_var.get().strip(),
            api_token=self.token_var.get().strip(),
        )
        if not current.api_token:
            raise ApiError(401, "API 토큰이 설정되지 않았습니다.")
        return FileSortingApiClient(current)

    # -- update check -----------------------------------------------------
    def _check_for_updates_async(self) -> None:
        # Self-reschedules (same pattern as _drain_ui_queue) so a long-lived
        # instance keeps noticing new versions instead of only checking once
        # at launch -- see _UPDATE_RECHECK_MS. Deliberately doesn't skip
        # itself just because a banner is already showing: on Windows/macOS
        # the "update" button only opens a download page (see _apply_update)
        # and never clears _pending_update, so gating on that would mean a
        # user who doesn't immediately click through never gets re-reminded.
        # check_for_update() is a single cheap read-only GET either way.
        self.root.after(self._UPDATE_RECHECK_MS, self._check_for_updates_async)

        base_url = self.base_url_var.get().strip()
        if not base_url:
            return

        def _run() -> None:
            info = check_for_update(base_url)
            if info:
                self.root.after(0, lambda: self._show_update_banner(info))

        threading.Thread(target=_run, daemon=True).start()

    def _show_update_banner(self, info: UpdateInfo) -> None:
        self._pending_update = info
        self.update_label["text"] = f"새 버전 {info.latest_version} 사용 가능 (현재 {info.current_version})"
        self.update_btn["text"] = "지금 업데이트" if info.can_self_apply else "다운로드 페이지 열기"
        self.update_btn.grid(row=3, column=2, sticky="w", padx=4)

    def _apply_update(self) -> None:
        info: Optional[UpdateInfo] = getattr(self, "_pending_update", None)
        if not info:
            return
        if info.can_self_apply:
            if not messagebox.askyesno(
                "업데이트", f"{info.latest_version} 버전으로 업데이트하고 앱을 재시작할까요?"
            ):
                return
            try:
                apply_update_linux(info)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("업데이트 실패", str(exc))
        else:
            webbrowser.open(info.download_url)

    # -- upload tab -----------------------------------------------------
    def _build_upload_tab(self) -> None:
        top = ttk.Frame(self.upload_tab)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="감시할 폴더").grid(row=0, column=0, sticky="w")
        self.watch_dir_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.watch_dir_var, width=45).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="찾아보기", command=self._pick_watch_dir).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="동시 업로드 개수").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.upload_concurrency_var = tk.IntVar(value=4)
        ttk.Spinbox(top, from_=1, to=16, textvariable=self.upload_concurrency_var, width=6).grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )
        top.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.upload_tab)
        btns.pack(fill="x", padx=8)
        self.upload_start_btn = ttk.Button(btns, text="자동 업로드 시작", command=self._start_watcher)
        self.upload_start_btn.pack(side="left")
        self.upload_stop_btn = ttk.Button(
            btns, text="중지", command=self._stop_watcher, state="disabled"
        )
        self.upload_stop_btn.pack(side="left", padx=4)

        self.upload_log = scrolledtext.ScrolledText(self.upload_tab, height=16, state="disabled")
        self.upload_log.pack(fill="both", expand=True, padx=8, pady=8)

    def _pick_watch_dir(self) -> None:
        chosen = filedialog.askdirectory()
        if chosen:
            self.watch_dir_var.set(chosen)

    def _start_watcher(self) -> None:
        watch_dir = self.watch_dir_var.get().strip()
        if not watch_dir:
            messagebox.showwarning("폴더 필요", "감시할 폴더를 먼저 선택해주세요.")
            return
        try:
            client = self._make_client()
        except ApiError as exc:
            messagebox.showerror("설정 오류", str(exc))
            return

        self._watcher = UploadWatcher(
            client=client,
            watch_dir=Path(watch_dir),
            concurrency=self.upload_concurrency_var.get(),
            on_event=lambda event: self._ui_queue.put(
                ("upload", f"{'✅' if event.ok else '❌'} {event.path.name} - {event.message}")
            ),
        )
        threading.Thread(target=self._run_watcher, daemon=True).start()
        self.upload_start_btn["state"] = "disabled"
        self.upload_stop_btn["state"] = "normal"
        self._ui_queue.put(("upload", f"▶ 감시 시작: {watch_dir}"))

    def _run_watcher(self) -> None:
        assert self._watcher is not None
        try:
            self._watcher.run_forever()
        except Exception as exc:  # noqa: BLE001
            self._ui_queue.put(("upload", f"❌ 감시 중 오류: {exc}"))
        finally:
            if self._watcher is not None:
                self._watcher.client.close()

    def _stop_watcher(self) -> None:
        if self._watcher:
            self._watcher.stop()
        self.upload_start_btn["state"] = "normal"
        self.upload_stop_btn["state"] = "disabled"
        self._ui_queue.put(("upload", "⏹ 감시 중지"))

    # -- download tab -----------------------------------------------------
    def _build_download_tab(self) -> None:
        top = ttk.Frame(self.download_tab)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="서버 경로 (비워두면 전체)").grid(row=0, column=0, sticky="w")
        self.remote_path_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.remote_path_var, width=35).grid(row=0, column=1, sticky="we", padx=4)

        ttk.Label(top, text="저장 위치").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.download_dir_var = tk.StringVar(value=str(Path.home() / "FileSorting_Downloads"))
        ttk.Entry(top, textvariable=self.download_dir_var, width=35).grid(
            row=1, column=1, sticky="we", padx=4, pady=(6, 0)
        )
        ttk.Button(top, text="찾아보기", command=self._pick_download_dir).grid(row=1, column=2, pady=(6, 0))

        ttk.Label(top, text="동시 다운로드 개수").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.download_concurrency_var = tk.IntVar(value=4)
        ttk.Spinbox(top, from_=1, to=16, textvariable=self.download_concurrency_var, width=6).grid(
            row=2, column=1, sticky="w", pady=(6, 0)
        )
        top.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.download_tab)
        btns.pack(fill="x", padx=8)
        self.download_btn = ttk.Button(btns, text="다운로드 시작", command=self._start_download)
        self.download_btn.pack(side="left")

        self.download_log = scrolledtext.ScrolledText(self.download_tab, height=16, state="disabled")
        self.download_log.pack(fill="both", expand=True, padx=8, pady=8)

    def _pick_download_dir(self) -> None:
        chosen = filedialog.askdirectory()
        if chosen:
            self.download_dir_var.set(chosen)

    def _start_download(self) -> None:
        try:
            client = self._make_client()
        except ApiError as exc:
            messagebox.showerror("설정 오류", str(exc))
            return

        remote_path = self.remote_path_var.get().strip()
        output_dir = Path(self.download_dir_var.get().strip())
        concurrency = self.download_concurrency_var.get()
        self.download_btn["state"] = "disabled"
        self._ui_queue.put(("download", f"▶ 다운로드 시작: '{remote_path or '/'}' -> {output_dir}"))

        def _run() -> None:
            try:
                events = download_tree(
                    client,
                    remote_path,
                    output_dir,
                    concurrency=concurrency,
                    on_event=lambda e: self._ui_queue.put(
                        ("download", f"{'✅' if e.ok else '❌'} {e.remote_path}")
                    ),
                )
                ok = sum(1 for e in events if e.ok)
                self._ui_queue.put(("download", f"완료: {ok}/{len(events)} 파일"))
            except Exception as exc:  # noqa: BLE001
                self._ui_queue.put(("download", f"❌ 오류: {exc}"))
            finally:
                client.close()
                self._ui_queue.put(("download-done", ""))

        threading.Thread(target=_run, daemon=True).start()

    # -- sync tab -----------------------------------------------------
    def _build_sync_tab(self) -> None:
        ttk.Label(
            self.sync_tab,
            text=(
                "폴더를 서버와 계속 맞춰줍니다 (Nextcloud처럼): 로컬에만 있는 파일은 자동 업로드,\n"
                "서버에만 있는 파일(다른 사람이 올린 것 포함)은 자동 다운로드됩니다. 삭제는 하지 않습니다."
            ),
            justify="left",
            foreground="#666",
        ).pack(fill="x", padx=8, pady=(8, 0))

        top = ttk.Frame(self.sync_tab)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="동기화할 폴더").grid(row=0, column=0, sticky="w")
        self.sync_dir_var = tk.StringVar(value=self.settings.sync_dir or str(Path.home() / "FileSorting_Sync"))
        ttk.Entry(top, textvariable=self.sync_dir_var, width=40).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="찾아보기", command=self._pick_sync_dir).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="서버 경로 (비워두면 전체)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sync_remote_var = tk.StringVar(value=self.settings.sync_remote_path)
        ttk.Entry(top, textvariable=self.sync_remote_var, width=40).grid(
            row=1, column=1, sticky="we", padx=4, pady=(6, 0)
        )

        ttk.Label(top, text="동시 전송 개수").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.sync_concurrency_var = tk.IntVar(value=self.settings.sync_concurrency or 4)
        ttk.Spinbox(top, from_=1, to=16, textvariable=self.sync_concurrency_var, width=6).grid(
            row=2, column=1, sticky="w", pady=(6, 0)
        )
        top.columnconfigure(1, weight=1)

        self.auto_sync_var = tk.BooleanVar(value=self.settings.sync_auto_start)
        ttk.Checkbutton(
            top,
            text="이 폴더를 항상 켜둔 채로 자동 동기화 (앱 시작 시 버튼 없이 자동 실행)",
            variable=self.auto_sync_var,
            command=self._save_sync_settings,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.prune_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top,
            text="동기화 시 서버에 이미 없는(정리·삭제된) 옛 로컬 파일 자동 정리",
            variable=self.prune_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(
            top,
            text="⚠ 로컬 파일을 실제로 삭제합니다 — 서버가 한 번도 본 적 없는 새 파일은 절대 건드리지 않고, "
            "서버가 이미 확인 후 의도적으로 지운 내용만 정리합니다.",
            justify="left",
            foreground="#a66",
            wraplength=520,
        ).grid(row=5, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(self.sync_tab)
        btns.pack(fill="x", padx=8)
        self.sync_once_btn = ttk.Button(btns, text="지금 한 번 동기화", command=self._run_sync_once)
        self.sync_once_btn.pack(side="left")
        self.sync_start_btn = ttk.Button(btns, text="백그라운드 자동 동기화 시작", command=self._start_sync_loop)
        self.sync_start_btn.pack(side="left", padx=4)
        self.sync_stop_btn = ttk.Button(btns, text="중지", command=self._stop_sync_loop, state="disabled")
        self.sync_stop_btn.pack(side="left", padx=4)
        self.force_upload_btn = ttk.Button(btns, text="전체 강제 업로드", command=self._run_force_upload)
        self.force_upload_btn.pack(side="left", padx=4)

        self.sync_log = scrolledtext.ScrolledText(self.sync_tab, height=14, state="disabled")
        self.sync_log.pack(fill="both", expand=True, padx=8, pady=8)

    def _pick_sync_dir(self) -> None:
        chosen = filedialog.askdirectory()
        if chosen:
            self.sync_dir_var.set(chosen)

    def _sync_params(self):
        sync_dir = self.sync_dir_var.get().strip()
        if not sync_dir:
            messagebox.showwarning("폴더 필요", "동기화할 폴더를 먼저 선택해주세요.")
            return None
        Path(sync_dir).mkdir(parents=True, exist_ok=True)
        try:
            client = self._make_client()
        except ApiError as exc:
            messagebox.showerror("설정 오류", str(exc))
            return None
        return client, Path(sync_dir), self.sync_remote_var.get().strip(), self.sync_concurrency_var.get()

    def _save_sync_settings(self) -> None:
        # Persists whatever's currently in the sync tab fields (not just the
        # auto-start flag) -- so picking a folder and starting the loop once
        # is enough for it to come back on the next launch, without a
        # separate "save" step the user has to remember.
        self.settings = self.settings.with_overrides(
            sync_dir=self.sync_dir_var.get().strip(),
            sync_remote_path=self.sync_remote_var.get().strip(),
            sync_concurrency=self.sync_concurrency_var.get(),
            sync_auto_start=self.auto_sync_var.get(),
        )
        self.settings.save()

    def _run_sync_once(self) -> None:
        params = self._sync_params()
        if not params:
            return
        client, sync_dir, remote_path, concurrency = params
        self.sync_once_btn["state"] = "disabled"
        self._ui_queue.put(("sync", f"▶ 동기화 시작: {sync_dir}"))

        prune = self.prune_var.get()

        def _run() -> None:
            try:
                events = run_sync(
                    client,
                    sync_dir,
                    remote_path,
                    concurrency=concurrency,
                    prune=prune,
                    on_event=lambda e: self._ui_queue.put(
                        ("sync", f"{'✅' if e.ok else '⚠️'} [{e.kind}] {e.label} {e.message}")
                    ),
                )
                self._ui_queue.put(("sync", f"완료: {len(events)}건 처리"))
            except Exception as exc:  # noqa: BLE001
                self._ui_queue.put(("sync", f"❌ 오류: {exc}"))
            finally:
                client.close()
                self._ui_queue.put(("sync-once-done", ""))

        threading.Thread(target=_run, daemon=True).start()

    def _run_force_upload(self) -> None:
        """Uploads every file in the sync folder unconditionally, ignoring
        the normal "server already has this" check -- a safety-net button
        for "make sure nothing in this folder was ever missed", separate
        from (and less trusting than) the everyday sync path. See
        force_upload_folder's docstring for why this is safe to run even
        against a huge old local backlog without re-inflating server disk
        usage: content the server already evaluated and deliberately
        removed gets discarded again immediately, no classify() call, no
        physical copy replanted -- only genuinely new content is filed.
        """
        params = self._sync_params()
        if not params:
            return
        client, sync_dir, _remote_path, concurrency = params

        local_files = list_local_files(sync_dir)
        if not local_files:
            client.close()
            messagebox.showinfo("강제 업로드", "폴더에 파일이 없습니다.")
            return
        total_bytes = sum(p.stat().st_size for p in local_files.values() if p.is_file())
        confirmed = messagebox.askyesno(
            "전체 강제 업로드",
            f"{sync_dir}\n\n{len(local_files)}개 파일 (약 {total_bytes / 1e9:.2f}GB)을 서버에 조건 없이 "
            "전부 업로드합니다. 서버가 이미 알고 있는 내용이어도 다시 보냅니다.\n\n계속할까요?",
        )
        if not confirmed:
            client.close()
            return

        self.force_upload_btn["state"] = "disabled"
        self._ui_queue.put(("sync", f"▶ 전체 강제 업로드 시작: {sync_dir} ({len(local_files)}개)"))

        def _run() -> None:
            try:
                events = force_upload_folder(
                    client,
                    sync_dir,
                    concurrency=concurrency,
                    on_event=lambda e: self._ui_queue.put(
                        ("sync", f"{'✅' if e.ok else '⚠️'} [{e.kind}] {e.label} {e.message}")
                    ),
                )
                self._ui_queue.put(("sync", f"강제 업로드 완료: {len(events)}건 처리"))
            except Exception as exc:  # noqa: BLE001
                self._ui_queue.put(("sync", f"❌ 오류: {exc}"))
            finally:
                client.close()
                self._ui_queue.put(("force-upload-done", ""))

        threading.Thread(target=_run, daemon=True).start()

    def _start_sync_loop(self) -> None:
        if self._sync_loop is not None:
            return  # already running (e.g. auto-started at launch) -- don't double it up
        params = self._sync_params()
        if not params:
            return
        client, sync_dir, remote_path, concurrency = params
        self._save_sync_settings()

        self._sync_loop = SyncLoop(
            client=client,
            local_dir=sync_dir,
            remote_path=remote_path,
            concurrency=concurrency,
            prune=self.prune_var.get(),
            on_event=lambda e: self._ui_queue.put(
                ("sync", f"{'✅' if e.ok else '⚠️'} [{e.kind}] {e.label} {e.message}")
            ),
        )
        threading.Thread(target=self._run_sync_loop, daemon=True).start()
        self.sync_start_btn["state"] = "disabled"
        self.sync_stop_btn["state"] = "normal"
        self._ui_queue.put(("sync", f"▶ 백그라운드 동기화 시작: {sync_dir}"))

    def _run_sync_loop(self) -> None:
        loop = self._sync_loop
        assert loop is not None
        try:
            loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            self._ui_queue.put(("sync", f"❌ 동기화 중 오류: {exc}"))
        finally:
            loop.client.close()
            if self._sync_loop is loop:  # not already cleared/replaced by a stop/restart
                self._sync_loop = None

    def _stop_sync_loop(self) -> None:
        if self._sync_loop:
            self._sync_loop.stop()
            self._sync_loop = None  # allow an immediate restart; the thread finishes on its own shortly after
        self.sync_start_btn["state"] = "normal"
        self.sync_stop_btn["state"] = "disabled"
        self._ui_queue.put(("sync", "⏹ 백그라운드 동기화 중지"))

    def _maybe_auto_start_sync(self) -> None:
        # Runs once, shortly after launch. Deliberately silent on failure
        # (no messagebox) -- this fires unattended, often minimized to tray
        # or right after autostart.py launched the app at login, so popping
        # a dialog nobody's there to dismiss would just be a stuck window.
        # Errors go to the sync log instead, and the loop itself already
        # retries forever regardless (see SyncLoop.run_forever).
        if not self.settings.sync_auto_start or self._sync_loop is not None:
            return
        if not self.sync_dir_var.get().strip() or not self.settings.api_token:
            return
        self._ui_queue.put(("sync", "▶ 저장된 설정으로 자동 동기화 시작"))
        self._start_sync_loop()

    # -- shared plumbing -----------------------------------------------------
    def _drain_ui_queue(self) -> None:
        try:
            while True:
                target, message = self._ui_queue.get_nowait()
                if target in ("download-done", "sync-once-done", "force-upload-done"):
                    if target == "download-done":
                        self.download_btn["state"] = "normal"
                    elif target == "sync-once-done":
                        self.sync_once_btn["state"] = "normal"
                    else:
                        self.force_upload_btn["state"] = "normal"
                    continue
                widget = {"upload": self.upload_log, "download": self.download_log, "sync": self.sync_log}[target]
                widget["state"] = "normal"
                widget.insert("end", message + "\n")
                widget.see("end")
                widget["state"] = "disabled"
        except queue.Empty:
            pass
        self.root.after(150, self._drain_ui_queue)

    # -- tray / background lifecycle -----------------------------------------
    def _setup_tray(self) -> None:
        try:
            import pystray
        except Exception:
            # No tray backend available (e.g. headless Linux with no
            # X11/AppIndicator) -- fall back to a normal window-close-quits
            # app rather than trapping the user with no way to exit.
            return

        menu = pystray.Menu(
            pystray.MenuItem("열기", self._tray_show, default=True),
            pystray.MenuItem("지금 동기화", self._tray_sync_now),
            pystray.MenuItem("종료", self._tray_quit),
        )
        self._tray_icon = pystray.Icon("file-sorting-uploader", _tray_icon_image(), "File Sorting Uploader", menu)
        # icon.run() blocks and, per pystray's own docs, "must be called from
        # the main thread" -- running it on a background thread (as this used
        # to do) works by accident on Windows/Linux (those backends pump
        # their own message loop/X11 connection either way) but hard-crashes
        # on macOS: pystray's darwin backend calls NSApplication.run(), and
        # AppKit now asserts that's a main-thread-only call (confirmed via a
        # user's crash report: NSUpdateCycleInitialize() is called off the
        # main thread, EXC_BREAKPOINT in -[NSApplication run] on a non-main
        # thread). run_detached() is pystray's documented answer for
        # integrating with a *different* library's mainloop -- here, Tk's
        # root.mainloop() at the bottom of main() -- instead of running its
        # own: on macOS it just marks the status item ready and piggybacks on
        # Tk's Aqua event loop (which shares the same NSApplication), and on
        # Windows/Linux it still spins its own thread internally, so this is
        # a strict, cross-platform-safe upgrade over the old manual thread.
        self._tray_icon.run_detached()

    def _tray_show(self, icon=None, item=None) -> None:
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def _tray_sync_now(self, icon=None, item=None) -> None:
        self.root.after(0, self._run_sync_once)

    def _tray_quit(self, icon=None, item=None) -> None:
        self.root.after(0, self._real_quit)

    def _on_close(self) -> None:
        if self._tray_icon is not None:
            # Background operation is the point (see module docstring) --
            # closing the window hides it, it doesn't stop anything running.
            self.root.withdraw()
        else:
            self._real_quit()

    def _real_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self._watcher:
            self._watcher.stop()
        if self._sync_loop:
            self._sync_loop.stop()
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FileSortingClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
