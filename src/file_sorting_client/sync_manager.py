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

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Tuple

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.download_manager import list_files_recursive
from file_sorting_client import folder_snapshot
from file_sorting_client.folder_snapshot import _list_local, _local_hashes
from file_sorting_client.models import BrowseEntry


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
    # (old_rel, new_rel) pairs the local folder-structure snapshot detected
    # as a local move/rename since the last diff_folder call -- see
    # folder_snapshot.py. Purely informational: content-hash matching
    # already makes this a no-op either way (see module docstring), this
    # just makes it visible instead of silent.
    moved: List[Tuple[str, str]] = field(default_factory=list)
    # rel_path -> local folder-mate filenames, from the current snapshot --
    # attached to uploads so the server's classify() call gets real local
    # folder context. See folder_snapshot.siblings_by_rel.
    sibling_map: Dict[str, List[str]] = field(default_factory=dict)
    # rel_path -> local folder's own name, from the current snapshot --
    # see folder_snapshot.folder_hint_by_rel.
    folder_hint_map: Dict[str, str] = field(default_factory=dict)
    # rel_path -> every path under that file's top-level local folder --
    # see folder_snapshot.subtree_by_rel.
    subtree_map: Dict[str, List[str]] = field(default_factory=dict)

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


def _rel_to_remote_base(entry_path: str, remote_base: str) -> str:
    if not remote_base:
        return entry_path
    prefix = remote_base.rstrip("/") + "/"
    return entry_path[len(prefix):] if entry_path.startswith(prefix) else entry_path


def _nudge_server_scan(client: FileSortingApiClient) -> None:
    """After uploading, ask the server to scan right away instead of
    silently leaving newly-uploaded files to wait for its own background
    poll loop -- which only runs every poll_interval_seconds (8s by
    default) to begin with. From the desktop client, that meant a sync/
    force-upload could report "완료" while the files sat untouched with
    nothing to show for it -- exactly the "did this actually do anything"
    confusion the web dashboard's own upload flow already avoids by making
    this same call.

    Calls twice, not once: the server's stability check (see worker.py's
    _scan_and_process) only starts processing a file once it's been
    observed with an unchanged size+mtime across two separate scans -- by
    design, so a file that's still mid-copy into watch_dir is never picked
    up half-written. A single scan-once call only registers that first
    observation; confirmed live that left a freshly-uploaded file still
    waiting on the server's own next background poll (up to
    poll_interval_seconds later) for the second one. Since the upload
    already completed synchronously over HTTP before this ever runs, the
    file's on-disk size/mtime are already final -- two immediate,
    back-to-back calls here safely satisfy that same check right away
    instead of waiting on it.

    Best-effort throughout: if this fails (older server without the
    endpoint, a transient error), the files are already safely uploaded
    regardless, and the server's own poll loop picks them up on schedule
    either way -- never worth failing the whole sync/upload over.
    """
    for _ in range(2):
        try:
            client.scan_once()
        except ApiError:
            return


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

    # Structure snapshot: reuses the hashes just computed above rather than
    # re-hashing (folder_snapshot.build_snapshot would otherwise redo the
    # exact same work). Diffed against the previous run's snapshot to
    # surface local moves/renames (see folder_snapshot.py), then persisted
    # for next time.
    current_snapshot = {
        rel: {"sha256": sha, "size": local_files[rel].stat().st_size}
        for rel, sha in local_sha_by_rel.items()
        if local_files[rel].is_file()
    }
    previous_snapshot = folder_snapshot.load_snapshot(local_dir)
    structure_diff = folder_snapshot.diff_snapshots(previous_snapshot, current_snapshot)
    folder_snapshot.save_snapshot(local_dir, current_snapshot)
    sibling_map = folder_snapshot.siblings_by_rel(current_snapshot)
    folder_hint_map = folder_snapshot.folder_hint_by_rel(current_snapshot)
    subtree_map = folder_snapshot.subtree_by_rel(current_snapshot)

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
        remote_entry = remote_by_rel.get(rel)

        # Check what's CURRENTLY at this exact relative path on the server
        # first, before the "is this content known anywhere on the server"
        # shortcut below -- content matching an OLD, since-superseded
        # revision is correctly "known" (existing_shas includes archived
        # versions, see api.check_hashes_detailed), so the shortcut would
        # otherwise silently swallow the one signal that actually matters
        # here: a newer revision now lives at this exact path and the local
        # copy is stale. Confirmed live against a real server: a local file
        # byte-identical to an archived old version, sitting at the same
        # path a newer version now occupies, produced neither an upload,
        # nor a download, nor a conflict -- nothing at all -- until this
        # check moved ahead of the shortcut.
        if remote_entry is not None and local_sha and remote_entry.sha256 and remote_entry.sha256 != local_sha:
            plan.conflicts.append(rel)
            continue
        if (
            remote_entry is not None
            and not remote_entry.sha256
            and remote_entry.size is not None
            and remote_entry.size != local_path.stat().st_size
        ):
            # Neither side has a usable hash for this exact path -- fall
            # back to the weaker size-only signal rather than silently
            # treating an unreadable/unhashed file as a non-conflict.
            plan.conflicts.append(rel)
            continue

        if local_sha and local_sha in existing_shas:
            # This content already exists somewhere on the server (and, per
            # the check above, it's not stale content sitting under a path
            # a newer revision now occupies) -- almost certainly this very
            # file, filed under a different path after classification.
            # Nothing to do.
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
        if remote_entry is None:
            plan.to_upload.append(local_path)
        # else: a remote entry exists at this path and (per the checks
        # above) its content matches -- nothing to do.

    for rel, entry in remote_by_rel.items():
        if rel in local_files:
            continue
        if entry.sha256 and entry.sha256 in local_shas:
            # Already have this content locally under a different path --
            # downloading it again would just create a duplicate copy.
            continue
        plan.to_download.append(entry)

    plan.moved = structure_diff.moved
    plan.sibling_map = sibling_map
    plan.folder_hint_map = folder_hint_map
    plan.subtree_map = subtree_map
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

    # Sibling/folder-hint context only needs the folder shape (which rel
    # paths share a parent), not hashes -- skip hashing entirely here,
    # consistent with force_upload's whole point of not bothering with the
    # normal does-the-server-already-know-this check either.
    snapshot_shape = {rel: {} for rel in local_files}
    sibling_map = folder_snapshot.siblings_by_rel(snapshot_shape)
    folder_hint_map = folder_snapshot.folder_hint_by_rel(snapshot_shape)
    subtree_map = folder_snapshot.subtree_by_rel(snapshot_shape)

    def _emit(event: SyncEvent) -> None:
        events.append(event)
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    def _upload_one(local_path: Path) -> None:
        try:
            rel = local_path.relative_to(local_dir).as_posix()
            siblings = sibling_map.get(rel)
            folder_hint = folder_hint_map.get(rel)
            subtree = subtree_map.get(rel)
            files_siblings = {local_path.name: siblings} if siblings else None
            files_hint = {local_path.name: folder_hint} if folder_hint else None
            files_subtree = {local_path.name: subtree} if subtree else None
            client.upload_files(
                [local_path], sibling_map=files_siblings, folder_hint_map=files_hint, subtree_map=files_subtree
            )
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

    _nudge_server_scan(client)
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

    for old_rel, new_rel in plan.moved:
        _emit(SyncEvent(
            "moved", new_rel, ok=True,
            message=f"로컬에서 이동/이름변경 감지됨 (이전 경로: {old_rel}) -- 내용은 이미 서버에 반영되어 있어 별도 조치 없음",
        ))

    def _upload_one(local_path: Path) -> None:
        try:
            rel = local_path.relative_to(local_dir).as_posix()
            siblings = plan.sibling_map.get(rel)
            folder_hint = plan.folder_hint_map.get(rel)
            subtree = plan.subtree_map.get(rel)
            sibling_map = {local_path.name: siblings} if siblings else None
            folder_hint_map = {local_path.name: folder_hint} if folder_hint else None
            subtree_map = {local_path.name: subtree} if subtree else None
            client.upload_files(
                [local_path], sibling_map=sibling_map, folder_hint_map=folder_hint_map, subtree_map=subtree_map
            )
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

    if plan.to_upload:
        _nudge_server_scan(client)

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
