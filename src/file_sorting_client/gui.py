"""Cross-platform desktop client (Windows/Linux/macOS) for the Auto File
Processor: a Tkinter GUI wrapping upload_watcher.py (concurrent auto-upload
of a local folder) and download_manager.py (concurrent recursive download
of the organized output). Pure Tkinter + stdlib/this package's own deps, so
the exact same source builds into a native binary on all three OSes via
PyInstaller (see client/build/*.sh, client/windows/build-manager.ps1) --
unlike windows/manager.py, nothing here touches a Windows-only API, so this
module has no platform guard on launch.

On Windows only, an extra "Mount (Windows)" tab is shown that reuses the
existing rclone-based WebDAV mount from windows/mount.py -- that mechanism
depends on WinFsp/rclone and isn't something Linux/macOS can share, so it
stays exactly as it already was rather than being ported here.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.config import ClientSettings
from file_sorting_client.download_manager import download_tree
from file_sorting_client.upload_watcher import UploadWatcher


def is_windows() -> bool:
    import platform

    return platform.system() == "Windows"


class FileSortingClientApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("File Sorting Client")
        self.root.geometry("720x560")

        self.settings = ClientSettings.load()
        self._watcher: Optional[UploadWatcher] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        self._build_settings_frame()
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.upload_tab = ttk.Frame(notebook)
        self.download_tab = ttk.Frame(notebook)
        notebook.add(self.upload_tab, text="자동 업로드")
        notebook.add(self.download_tab, text="다운로드")

        self._build_upload_tab()
        self._build_download_tab()

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
        frame.columnconfigure(1, weight=1)

    def _save_settings(self) -> None:
        self.settings = self.settings.with_overrides(
            api_base_url=self.base_url_var.get().strip(),
            api_token=self.token_var.get().strip(),
        )
        self.settings.save()
        messagebox.showinfo("저장 완료", f"설정을 저장했습니다:\n{self.settings.config_file}")

    def _make_client(self) -> FileSortingApiClient:
        current = self.settings.with_overrides(
            api_base_url=self.base_url_var.get().strip(),
            api_token=self.token_var.get().strip(),
        )
        if not current.api_token:
            raise ApiError(401, "API 토큰이 설정되지 않았습니다.")
        return FileSortingApiClient(current)

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

        self.upload_log = scrolledtext.ScrolledText(self.upload_tab, height=18, state="disabled")
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
        self._watcher_thread = threading.Thread(target=self._run_watcher, daemon=True)
        self._watcher_thread.start()
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

        self.download_log = scrolledtext.ScrolledText(self.download_tab, height=18, state="disabled")
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

    # -- shared plumbing -----------------------------------------------------
    def _drain_ui_queue(self) -> None:
        try:
            while True:
                target, message = self._ui_queue.get_nowait()
                if target == "download-done":
                    self.download_btn["state"] = "normal"
                    continue
                widget = self.upload_log if target == "upload" else self.download_log
                widget["state"] = "normal"
                widget.insert("end", message + "\n")
                widget.see("end")
                widget["state"] = "disabled"
        except queue.Empty:
            pass
        self.root.after(150, self._drain_ui_queue)

    def _on_close(self) -> None:
        if self._watcher:
            self._watcher.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FileSortingClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
