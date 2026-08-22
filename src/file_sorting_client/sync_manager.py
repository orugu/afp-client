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
    # Local files whose content the server has already seen and
    # deliberately removed (see diff_folder's include_prune) -- only ever
    # populated when explicitly requested, and only ever acted on by
    # run_sync when its own `prune` flag is also set. Never populated by a
    # normal sync cycle.
    to_prune: List[Path] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.to_upload or self.to_download or self.to_prune)


@dataclass
class SyncEvent:
    kind: str  # "upload" | "download"
    label: str
    ok: bool
    message: str = ""


def list_local_files(local_dir: Path) -> Dict[str, Path]:
    """Public alias for _list_local -- for callers (the GUI's confirmation
    dialogs, mainly) that just want "what's in this folder right now" to
    show a count/size before an action like force-upload or prune, without
    reaching into a private helper.
    """
    return _list_local(local_dir)


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


def diff_folder(
    client: FileSortingApiClient, local_dir: Path, remote_path: str = "", *, include_prune: bool = False
) -> SyncPlan:
    """Read-only comparison -- safe to call as often as you like, e.g. to
    preview a sync before running it. See module docstring for why this
    matches by content hash rather than relative path.

    include_prune: also populate plan.to_prune with local files whose
    content the server has already seen and deliberately removed (as
    opposed to content it's simply never seen). Off by default -- this
    costs nothing extra to compute (same check-hashes round trip either
    way) but is opt-in because *acting* on to_prune deletes local files,
    and a caller that doesn't know to check it shouldn't get a plan that
    quietly implies deletion.
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

    # /files/browse deliberately skips _trash (server-side, so the folder
    # view stays clean), so content that was correctly deduped into trash
    # never shows up in remote_shas even though it genuinely still exists
    # server-side. Without this, that content looks "missing" forever and
    # gets re-uploaded on every single cycle -- ask the server directly
    # instead of trusting the browse walk alone. See api.check_hashes.
    ever_seen_shas: set = set()
    try:
        existing_from_check, ever_seen_shas = client.check_hashes_detailed(list(local_sha_by_rel.values()))
        existing_shas = remote_shas | existing_from_check
    except ApiError:
        # Older server without this endpoint yet, or a transient failure --
        # fall back to the (less complete) browse-only view rather than
        # blocking sync entirely.
        existing_shas = remote_shas

    plan = SyncPlan()
    for rel, local_path in local_files.items():
        local_sha = local_sha_by_rel.get(rel)
        if local_sha and local_sha in existing_shas:
            # This content already exists somewhere on the server -- almost
            # certainly this very file, filed under a different path after
            # classification. Nothing to do.
            continue
        if include_prune and local_sha and local_sha in ever_seen_shas:
            # Not live anywhere on the server right now, but the server HAS
            # seen this content before (any status, including 'deleted') --
            # this is old local backlog the server already evaluated and
            # deliberately removed, e.g. a folder that was last synced
            # before a server-side dedup cleanup ran. Never uploaded
            # content (ever_seen doesn't contain it) always goes to
            # to_upload instead, further down -- prune only ever targets
            # content the server has explicitly already made a decision
            # about.
            plan.to_prune.append(local_path)
            continue
        remote_entry = remote_by_rel.get(rel)
        if remote_entry is None:
            plan.to_upload.append(local_path)
        elif local_sha:
            # local_sha is confirmed NOT to match any content the server
            # has anywhere (the existing_shas check above already
            # `continue`d if it did) -- so a remote entry existing at this
            # same relative path always means genuinely different content
            # now, not just a coincidentally matching size. Previously this
            # only flagged a conflict when sizes also differed, which
            # silently ignored a same-size-different-content mismatch
            # forever. That's exactly the shape a version bump can take: a
            # newer server-side revision landing back at the same served
            # path (see the version-tracking feature) with about the same
            # size as what this file used to be -- would have gone
            # unnoticed here indefinitely, with no upload, no download, and
            # no conflict ever surfaced.
            plan.conflicts.append(rel)
        elif remote_entry.size is not None and remote_entry.size != local_path.stat().st_size:
            # Local file couldn't be hashed (e.g. a transient IO error) --
            # fall back to the weaker size-only signal rather than silently
            # treating an unreadable file as a non-conflict.
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


def force_upload_folder(
    client: FileSortingApiClient,
    local_dir: Path,
    *,
    concurrency: int = 4,
    on_event: Optional[Callable[[SyncEvent], None]] = None,
) -> List[SyncEvent]:
    """Uploads every file under `local_dir`, skipping the normal "does the
    server already have this content" check entirely -- a deliberate
    escape hatch for when you don't trust that check (or just want an
    unconditional guarantee) rather than the everyday sync path.

    Safe to run even against a huge, mostly-already-known local backlog:
    the server-side ingest pipeline (see worker._record_known_deleted_
    duplicate) recognizes content that was already evaluated and
    intentionally purged before, and discards those re-uploads immediately
    without spending a classify() call or planting a physical copy again
    -- so this doesn't re-inflate server disk usage just because the local
    folder is carrying old duplicate weight. Content the server has never
    seen still gets filed normally, which is the whole point: a safety net
    for "did anything genuinely unique in this folder ever make it up?"
    """
    local_files = _list_local(local_dir)
    events: List[SyncEvent] = []

    def _emit(event: SyncEvent) -> None:
        events.append(event)
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    def _upload_one(local_path: Path) -> None:
        try:
            client.upload_files([local_path])
            _emit(SyncEvent("upload", local_path.name, ok=True, message="강제 업로드됨"))
        except (ApiError, OSError) as exc:
            _emit(SyncEvent("upload", local_path.name, ok=False, message=str(exc)))

    if not local_files:
        return events

    workers = min(concurrency, len(local_files))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_upload_one, p) for p in local_files.values()]
        for future in as_completed(futures):
            future.result()

    return events


def run_sync(
    client: FileSortingApiClient,
    local_dir: Path,
    remote_path: str = "",
    *,
    concurrency: int = 4,
    prune: bool = False,
    on_event: Optional[Callable[[SyncEvent], None]] = None,
) -> List[SyncEvent]:
    """prune: also delete local files whose content the server has already
    seen and deliberately removed (see diff_folder's include_prune) --
    e.g. a local folder that was last synced before a server-side dedup
    cleanup ran, still carrying gigabytes the server no longer has any
    record of wanting. Off by default: this is the one thing anywhere in
    this module that deletes local files, so it only ever happens when a
    caller explicitly opts in for this one call, never as a side effect of
    an ordinary sync cycle. Content the server has genuinely never seen is
    never touched -- it always goes to plan.to_upload instead.
    """
    plan = diff_folder(client, local_dir, remote_path, include_prune=prune)
    events: List[SyncEvent] = []

    def _emit(event: SyncEvent) -> None:
        events.append(event)
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    for rel in plan.conflicts:
        _emit(SyncEvent(
            "conflict", rel, ok=False,
            message="같은 경로에 내용이 다른 파일이 있어 건너뜀 (수동 확인 필요 -- 서버에 새 버전이 올라왔을 수 있습니다)",
        ))

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
    if tasks:
        workers = min(concurrency, len(tasks))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fn, arg) for fn, arg in tasks]
            for future in as_completed(futures):
                future.result()  # propagate unexpected exceptions; _upload_one/_download_one already catch the expected ones

    if prune:
        for local_path in plan.to_prune:
            try:
                local_path.unlink()
                _emit(SyncEvent("prune", local_path.name, ok=True, message="서버에 없는 옛 사본 삭제됨"))
            except OSError as exc:
                _emit(SyncEvent("prune", local_path.name, ok=False, message=str(exc)))

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
    prune: bool = False
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
                    prune=self.prune,
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
