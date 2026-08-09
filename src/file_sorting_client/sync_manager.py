"""Folder <-> server reconciliation: pick a local folder, compare it against
the server's organized output, and fill in whatever's missing on either
side -- local-only files get uploaded, remote-only files get downloaded.
Both directions run concurrently (thread pool), same pattern as
upload_watcher.py / download_manager.py.

Deliberately conservative: this never deletes or overwrites anything. A
relative path that exists on both sides is left alone even if sizes differ
(reported as a conflict, not resolved automatically) -- silently picking a
winner would mean occasionally destroying data with no way back. Filling
gaps is safe; guessing isn't.

One structural thing worth knowing before using this: uploaded files are
classified by the server (Gemini/OpenRouter decides the destination
category/subfolder), so a locally-uploaded file will generally NOT
reappear at the same relative path it was uploaded from -- matching by path
here only decides "does something need to happen for this file", not "this
exact path will exist on both sides after sync". That's inherent to how
this system organizes files, not a bug in this sync logic.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, List, Optional

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.download_manager import list_files_recursive
from file_sorting_client.models import BrowseEntry

_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass
class SyncPlan:
    to_upload: List[Path] = field(default_factory=list)
    to_download: List[BrowseEntry] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)  # same rel path, different size

    @property
    def is_empty(self) -> bool:
        return not (self.to_upload or self.to_download)


@dataclass
class SyncEvent:
    kind: str  # "upload" | "download"
    label: str
    ok: bool
    message: str = ""


def _list_local(local_dir: Path) -> dict:
    result = {}
    if not local_dir.is_dir():
        return result
    for p in local_dir.rglob("*"):
        if p.is_file() and p.name not in _IGNORED_NAMES and not p.name.startswith("."):
            rel = p.relative_to(local_dir).as_posix()
            result[rel] = p
    return result


def _rel_to_remote_base(entry_path: str, remote_base: str) -> str:
    if not remote_base:
        return entry_path
    prefix = remote_base.rstrip("/") + "/"
    return entry_path[len(prefix):] if entry_path.startswith(prefix) else entry_path


def diff_folder(client: FileSortingApiClient, local_dir: Path, remote_path: str = "") -> SyncPlan:
    """Read-only comparison -- safe to call as often as you like, e.g. to
    preview a sync before running it."""
    local_files = _list_local(local_dir)

    try:
        remote_entries = list_files_recursive(client, remote_path)
    except ApiError as exc:
        if exc.status_code == 404:
            remote_entries = []
        else:
            raise

    remote_by_rel = {}
    for entry in remote_entries:
        rel = _rel_to_remote_base(entry.path, remote_path)
        remote_by_rel[rel] = entry

    plan = SyncPlan()
    for rel, local_path in local_files.items():
        remote_entry = remote_by_rel.get(rel)
        if remote_entry is None:
            plan.to_upload.append(local_path)
        elif remote_entry.size is not None and remote_entry.size != local_path.stat().st_size:
            plan.conflicts.append(rel)

    local_rels = set(local_files.keys())
    for rel, entry in remote_by_rel.items():
        if rel not in local_rels:
            plan.to_download.append(entry)

    return plan


def run_sync(
    client: FileSortingApiClient,
    local_dir: Path,
    remote_path: str = "",
    *,
    concurrency: int = 4,
    on_event: Optional[Callable[[SyncEvent], None]] = None,
) -> List[SyncEvent]:
    plan = diff_folder(client, local_dir, remote_path)
    events: List[SyncEvent] = []

    def _emit(event: SyncEvent) -> None:
        events.append(event)
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    for rel in plan.conflicts:
        _emit(SyncEvent("conflict", rel, ok=False, message="같은 경로, 크기가 달라 건너뜀 (수동 확인 필요)"))

    def _upload_one(local_path: Path) -> None:
        try:
            client.upload_files([local_path])
            _emit(SyncEvent("upload", local_path.name, ok=True, message="업로드됨 (분류 후 위치가 바뀔 수 있음)"))
        except (ApiError, OSError) as exc:
            _emit(SyncEvent("upload", local_path.name, ok=False, message=str(exc)))

    def _download_one(entry: BrowseEntry) -> None:
        rel = _rel_to_remote_base(entry.path, remote_path)
        destination = local_dir / rel
        try:
            client.download_organized_file(entry.path, destination)
            _emit(SyncEvent("download", entry.path, ok=True))
        except (ApiError, OSError) as exc:
            _emit(SyncEvent("download", entry.path, ok=False, message=str(exc)))

    tasks = [(_upload_one, p) for p in plan.to_upload] + [(_download_one, e) for e in plan.to_download]
    if not tasks:
        return events

    workers = min(concurrency, len(tasks))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fn, arg) for fn, arg in tasks]
        for future in as_completed(futures):
            future.result()  # propagate unexpected exceptions; _upload_one/_download_one already catch the expected ones

    return events


@dataclass
class SyncLoop:
    """Continuous background version of run_sync -- re-diffs and reconciles
    on an interval, forever, until stop() is called. This is what makes the
    synced folder behave like a shared Nextcloud-style drive: whoever
    uploads (this app, another copy of it, someone else entirely through
    the web UI) shows up locally on the next cycle, and anything dropped
    into the local folder goes up on the next cycle too -- no manual
    "sync now" needed once this is running.
    """

    client: FileSortingApiClient
    local_dir: Path
    remote_path: str = ""
    concurrency: int = 4
    poll_interval_seconds: float = 15.0
    on_event: Optional[Callable[[SyncEvent], None]] = None

    _stop_event: Event = field(default_factory=Event, init=False, repr=False)

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        while not self._stop_event.wait(0):
            try:
                run_sync(
                    self.client,
                    self.local_dir,
                    self.remote_path,
                    concurrency=self.concurrency,
                    on_event=self.on_event,
                )
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                if self.on_event:
                    try:
                        self.on_event(SyncEvent("error", str(self.local_dir), ok=False, message=str(exc)))
                    except Exception:
                        pass
            if self._stop_event.wait(self.poll_interval_seconds):
                return
