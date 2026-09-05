from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Tuple

from file_sorting_client.api import ApiError, FileSortingApiClient

# Persists which local files have already been uploaded (by fingerprint), so
# restarting the watcher doesn't resend everything in the folder again. Kept
# next to the rest of this client's local state.
_STATE_DIR = Path.home() / ".config" / "file-sorting-client"
_STATE_FILE = _STATE_DIR / "upload_state.json"

# Ignore the same kind of noise the server-side worker ignores (partial
# downloads, OS metadata files) -- kept minimal and independent from the
# server's own file_filters.py since this runs standalone on the client.
_IGNORED_SUFFIXES = {".crdownload", ".part", ".tmp"}
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _is_ignorable(name: str) -> bool:
    if name in _IGNORED_NAMES or name.startswith("."):
        return True
    return Path(name).suffix.lower() in _IGNORED_SUFFIXES


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class UploadEvent:
    path: Path
    ok: bool
    message: str = ""


@dataclass
class UploadWatcher:
    """Watches a local folder and uploads new/changed files to the server's
    /files/upload endpoint, concurrently, using the same "stable across two
    polls" trick the server-side worker uses to avoid uploading a file
    that's still being written (a large copy, a browser download still in
    progress, etc.) -- see backend/app/worker.py::_scan_and_process.

    Concurrency here mirrors the server-side change: uploading N files no
    longer means N sequential HTTP round-trips, up to `concurrency` files
    transfer at once via a thread pool.
    """

    client: FileSortingApiClient
    watch_dir: Path
    concurrency: int = 4
    poll_interval_seconds: float = 3.0
    on_event: Optional[Callable[[UploadEvent], None]] = None
    state_file: Path = _STATE_FILE

    _stop_event: Event = field(default_factory=Event, init=False, repr=False)
    _pending_sizes: Dict[Path, Tuple[int, float]] = field(default_factory=dict, init=False, repr=False)
    _uploaded: Dict[str, Tuple[int, float]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._uploaded = self._load_state()

    def _load_state(self) -> Dict[str, Tuple[int, float]]:
        if not self.state_file.exists():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {k: (v[0], v[1]) for k, v in raw.items()}
        except Exception:
            return {}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._uploaded, indent=2), encoding="utf-8")

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        while not self._stop_event.wait(0):
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self._emit(UploadEvent(self.watch_dir, ok=False, message=f"scan error: {exc}"))
            if self._stop_event.wait(self.poll_interval_seconds):
                return

    def scan_once(self) -> List[UploadEvent]:
        if not self.watch_dir.is_dir():
            return []

        files = [p for p in self.watch_dir.iterdir() if p.is_file() and not _is_ignorable(p.name)]

        stable_files: List[Path] = []
        seen: Dict[Path, Tuple[int, float]] = {}
        for src in files:
            try:
                stat = src.stat()
            except OSError:
                continue
            fingerprint = (stat.st_size, stat.st_mtime)
            seen[src] = fingerprint
            if self._pending_sizes.get(src) != fingerprint:
                continue
            if self._uploaded.get(str(src)) == fingerprint:
                continue  # already uploaded this exact version
            stable_files.append(src)
        self._pending_sizes = seen

        if not stable_files:
            return []

        # Hash before uploading and ask the server which of these it
        # already has -- content identical to something already filed
        # doesn't need re-sending, even if the server's copy is sitting in
        # _trash as a known duplicate (which this watcher, tracking only
        # its own local upload history, has no other way to know about).
        # Without this, a watched folder that keeps regenerating the same
        # file under a new name (or was mirrored from the server before a
        # dedup cleanup) re-uploads the exact same bytes forever.
        hashes_by_src: Dict[Path, str] = {}
        for src in stable_files:
            try:
                hashes_by_src[src] = _sha256_of(src)
            except OSError:
                pass  # let _upload_one's own stat/open surface the real error
        try:
            already_on_server = self.client.check_hashes(list(set(hashes_by_src.values())))
        except ApiError:
            already_on_server = set()  # older server, or a transient failure -- don't block uploads on this

        events: List[UploadEvent] = []
        to_upload: List[Path] = []
        for src in stable_files:
            if hashes_by_src.get(src) in already_on_server:
                fingerprint = seen.get(src)
                if fingerprint:
                    self._uploaded[str(src)] = fingerprint
                event = UploadEvent(src, ok=True, message="이미 서버에 있는 내용이라 업로드 생략")
                events.append(event)
                self._emit(event)
            else:
                to_upload.append(src)

        if to_upload:
            # watch_dir is flat (no subfolders), so every other file
            # currently sitting in it is a genuine local folder-mate --
            # sent along so the server's classify() call gets real context
            # instead of none at all (see api.upload_files/main.py's
            # /api/files/upload `siblings` field).
            sibling_names = [p.name for p in files]
            workers = min(self.concurrency, len(to_upload))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._upload_one, src, sibling_names): src for src in to_upload
                }
                for future in as_completed(futures):
                    events.append(future.result())
            # Ask the server to scan right away rather than leave these
            # newly-uploaded files to wait for its own background poll
            # loop. Two calls, not one: the server only starts processing a
            # file once it's seen its size+mtime unchanged across two
            # separate scans (a still-being-written guard) -- a single call
            # only registers the first observation. Since the upload just
            # completed synchronously over HTTP, the file's on disk is
            # already final, so two immediate calls safely satisfy that
            # right away. Best-effort: the files are already safely
            # uploaded regardless of whether this succeeds.
            for _ in range(2):
                try:
                    self.client.scan_once()
                except ApiError:
                    break

        if any(e.ok for e in events):
            self._save_state()
        return events

    def _upload_one(self, src: Path, sibling_names: Optional[List[str]] = None) -> UploadEvent:
        try:
            stat = src.stat()
            fingerprint = (stat.st_size, stat.st_mtime)
            others = [n for n in (sibling_names or []) if n != src.name]
            sibling_map = {src.name: others} if others else None
            self.client.upload_files([src], sibling_map=sibling_map)
            self._uploaded[str(src)] = fingerprint
            event = UploadEvent(src, ok=True, message="uploaded")
        except ApiError as exc:
            event = UploadEvent(src, ok=False, message=str(exc))
        except OSError as exc:
            event = UploadEvent(src, ok=False, message=str(exc))
        self._emit(event)
        return event

    def _emit(self, event: UploadEvent) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
