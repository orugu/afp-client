from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.models import BrowseEntry


@dataclass
class DownloadEvent:
    remote_path: str
    ok: bool
    message: str = ""
    local_path: Optional[Path] = None


def list_files_recursive(client: FileSortingApiClient, remote_path: str = "") -> List[BrowseEntry]:
    """Walks the organized-output tree from `remote_path` down, returning
    every file entry found (directories are expanded, not returned). Used to
    turn "download this folder" into a flat list of individual files to
    fetch.
    """
    files: List[BrowseEntry] = []
    stack = [remote_path]
    while stack:
        current = stack.pop()
        result = client.browse(current)
        for entry in result.entries:
            if entry.type == "dir":
                stack.append(entry.path)
            else:
                files.append(entry)
    return files


def download_tree(
    client: FileSortingApiClient,
    remote_path: str,
    local_dir: Path,
    *,
    concurrency: int = 4,
    on_event: Optional[Callable[[DownloadEvent], None]] = None,
) -> List[DownloadEvent]:
    """Downloads a single file or an entire folder (recursively) from the
    server's organized output into `local_dir`, preserving the remote
    relative folder structure. Multiple files transfer concurrently via a
    thread pool -- the same pattern as upload_watcher.py and the server's
    own worker concurrency, just applied to the pull direction.
    """
    # No dedicated "stat a single path" endpoint exists server-side, so this
    # tries treating remote_path as a folder first (browse succeeds -> walk
    # it); a 404 there means it's a single file path instead.
    try:
        entries = list_files_recursive(client, remote_path)
    except ApiError as exc:
        if exc.status_code != 404:
            raise
        entries = [BrowseEntry(name=Path(remote_path).name, path=remote_path, type="file")]

    if not entries:
        return []

    events: List[DownloadEvent] = []
    workers = min(concurrency, len(entries))

    def _download_one(entry: BrowseEntry) -> DownloadEvent:
        rel = Path(entry.path)
        try:
            base = Path(remote_path) if remote_path else Path(".")
            rel_to_base = rel.relative_to(base) if remote_path and str(rel).startswith(remote_path) else rel
        except ValueError:
            rel_to_base = rel
        destination = local_dir / rel_to_base
        try:
            client.download_organized_file(entry.path, destination)
            event = DownloadEvent(entry.path, ok=True, local_path=destination)
        except ApiError as exc:
            event = DownloadEvent(entry.path, ok=False, message=str(exc))
        except OSError as exc:
            event = DownloadEvent(entry.path, ok=False, message=str(exc))
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass
        return event

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_download_one, entry) for entry in entries]
        for future in as_completed(futures):
            events.append(future.result())

    return events
