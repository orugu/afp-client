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

Matches by CONTENT (sha256), not by relative path. Uploaded files are
classified by the server (Gemini/OpenRouter decides the destination
category/subfolder), so a locally-uploaded file will generally NOT
reappear at the same relative path it was uploaded from. Path-only
matching used to mean a file's local rel path never found its remote
counterpart, so it looked "local-only" forever and got re-uploaded on
every single cycle -- and symmetrically, the server's reorganized copy
never matched anything locally either, so it kept "downloading" into a
newly nested path each time, multiplying copies. Comparing by sha256
instead means "does this content exist somewhere on the other side",
which is stable across the server's reorganization. Falls back to
path+size for entries the server hasn't hashed yet (e.g. very old rows).
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.download_manager import list_files_recursive
from file_sorting_client.models import BrowseEntry

_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Local cache of already-hashed files (keyed by relative path, invalidated
# by mtime+size) so a folder that isn't changing doesn't get fully re-read
# and re-hashed every poll cycle -- sync loops as often as every 15s.
_HASH_CACHE_FILENAME = ".afp_sync_hash_cache.json"


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


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_hash_cache(local_dir: Path) -> Dict[str, dict]:
    try:
        return json.loads((local_dir / _HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_hash_cache(local_dir: Path, cache: Dict[str, dict]) -> None:
    try:
        (local_dir / _HASH_CACHE_FILENAME).write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def _local_hashes(local_files: Dict[str, Path], local_dir: Path) -> Dict[str, str]:
    """rel path -> sha256 for every local file, reusing the on-disk cache
    for anything whose mtime+size hasn't changed since it was last hashed.
    """
    cache = _load_hash_cache(local_dir)
    result = {}
    changed = False
    for rel, path in local_files.items():
        try:
            stat = path.stat()
        except OSError:
            continue
        cached = cache.get(rel)
        if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
            result[rel] = cached["sha256"]
            continue
        try:
            digest = _sha256_of(path)
        except OSError:
            continue
        result[rel] = digest
        cache[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "sha256": digest}
        changed = True
    if changed:
        _save_hash_cache(local_dir, cache)
    return result


def diff_folder(client: FileSortingApiClient, local_dir: Path, remote_path: str = "") -> SyncPlan:
    """Read-only comparison -- safe to call as often as you like, e.g. to
    preview a sync before running it. See module docstring for why this
    matches by content hash rather than relative path.
    """
    local_files = _list_local(local_dir)
    local_sha_by_rel = _local_hashes(local_files, local_dir)
    local_shas = set(local_sha_by_rel.values())

    try:
        remote_entries = list_files_recursive(client, remote_path)
    except ApiError as exc:
        if exc.status_code == 404:
            remote_entries = []
        else:
            raise

    remote_by_rel = {}
    remote_shas = set()
    for entry in remote_entries:
        rel = _rel_to_remote_base(entry.path, remote_path)
        remote_by_rel[rel] = entry
        if entry.sha256:
            remote_shas.add(entry.sha256)

    plan = SyncPlan()
    for rel, local_path in local_files.items():
        local_sha = local_sha_by_rel.get(rel)
        if local_sha and local_sha in remote_shas:
            # This content already exists somewhere on the server -- almost
            # certainly this very file, filed under a different path after
            # classification. Nothing to do.
            continue
        remote_entry = remote_by_rel.get(rel)
        if remote_entry is None:
            plan.to_upload.append(local_path)
        elif remote_entry.size is not None and remote_entry.size != local_path.stat().st_size:
            plan.conflicts.append(rel)

    for rel, entry in remote_by_rel.items():
        if rel in local_files:
            continue
        if entry.sha256 and entry.sha256 in local_shas:
            # Already have this content locally under a different path --
            # downloading it again would just create a duplicate copy.
            continue
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
