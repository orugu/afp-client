"""Local folder-structure snapshot + diff.

sync_manager.py already tracks per-file content hashes (its own
`.afp_sync_hash_cache.json`) but only ever compares against the SERVER --
it has no memory of what the local folder itself looked like last time, so
it can't tell "this file just appeared" apart from "this file used to sit
somewhere else in this same folder" (a local rename/reorganization). That
distinction matters for two things this module exists to support:

  1. Visibility: surfacing "이 파일 로컬에서 이동/이름변경 감지" instead of
     silently treating a locally-moved file as a brand-new upload candidate
     (harmless today since sync_manager already matches by content hash --
     see its module docstring -- but invisible to the user otherwise).
  2. Folder context for the server: for files that ARE genuinely new,
     `siblings_by_rel` reports what else currently sits in the same local
     folder, sent alongside the upload (see api.py's upload_files
     `sibling_map` / main.py's /api/files/upload `siblings` field) so the
     server's classify() call sees real local structure instead of none at
     all -- previously true only for the server's own re-review pass, never
     for a file's first classification.

The local-listing/hashing helpers below (_list_local, _local_hashes, and
their hash-cache-file backing) used to live in sync_manager.py; they moved
here so sync_manager can build on this module without a circular import
(sync_manager needs this module's diffing, this module needs sync_manager's
listing) -- sync_manager now imports them back from here instead. Behavior
is unchanged; only the home module moved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

_SNAPSHOT_FILENAME = ".afp_directory_snapshot.json"
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Local cache of already-hashed files (keyed by relative path, invalidated
# by mtime+size) so a folder that isn't changing doesn't get fully re-read
# and re-hashed every poll cycle -- sync loops as often as every 15s.
_HASH_CACHE_FILENAME = ".afp_sync_hash_cache.json"


def _list_local(local_dir: Path) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    if not local_dir.is_dir():
        return result
    for p in local_dir.rglob("*"):
        if p.is_file() and p.name not in _IGNORED_NAMES and not p.name.startswith("."):
            rel = p.relative_to(local_dir).as_posix()
            result[rel] = p
    return result


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
    result: Dict[str, str] = {}
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


@dataclass
class StructureDiff:
    # Rel paths that exist now but weren't in the previous snapshot, AND
    # whose content isn't explained by a `moved` entry below.
    added: List[str] = field(default_factory=list)
    # Rel paths that were in the previous snapshot but no longer exist,
    # AND whose content isn't explained by a `moved` entry below.
    removed: List[str] = field(default_factory=list)
    # (old_rel, new_rel) pairs: same content (sha256), different path --
    # a local move/rename since the last snapshot.
    moved: List[Tuple[str, str]] = field(default_factory=list)
    # Rel paths present in both snapshots but whose content hash changed --
    # someone edited the file in place rather than replacing it.
    content_changed: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.moved or self.content_changed)


def build_snapshot(local_dir: Path) -> Dict[str, dict]:
    """rel_path -> {"sha256":..., "size":...} for every file currently
    under local_dir. Directories are implicit in the rel paths themselves
    (a snapshot never lists an empty folder -- there's nothing to diff
    about a folder with no files in it either way).
    """
    local_files = _list_local(local_dir)
    hashes = _local_hashes(local_files, local_dir)
    snapshot: Dict[str, dict] = {}
    for rel, path in local_files.items():
        sha256 = hashes.get(rel)
        if not sha256:
            continue  # unreadable at hash time; skip rather than record a half-entry
        try:
            size = path.stat().st_size
        except OSError:
            continue
        snapshot[rel] = {"sha256": sha256, "size": size}
    return snapshot


def load_snapshot(local_dir: Path) -> Dict[str, dict]:
    try:
        raw = json.loads((local_dir / _SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_snapshot(local_dir: Path, snapshot: Dict[str, dict]) -> None:
    try:
        (local_dir / _SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
        pass


def diff_snapshots(old: Dict[str, dict], new: Dict[str, dict]) -> StructureDiff:
    """Pure comparison, no filesystem access -- easy to unit test and
    reusable regardless of where the two snapshots came from.
    """
    old_rels = set(old.keys())
    new_rels = set(new.keys())

    unchanged_or_content_changed = old_rels & new_rels
    only_old = old_rels - new_rels
    only_new = new_rels - old_rels

    diff = StructureDiff()
    for rel in unchanged_or_content_changed:
        if old[rel].get("sha256") != new[rel].get("sha256"):
            diff.content_changed.append(rel)

    # A rel path that disappeared and one that appeared, sharing the same
    # content hash, is a move/rename rather than an independent
    # delete+add. Content hashes aren't guaranteed unique across
    # `only_old`/`only_new` (two old files could coincidentally share a
    # hash), so this pairs them up greedily and whatever's left over on
    # each side falls through to plain added/removed.
    old_by_sha: Dict[str, List[str]] = {}
    for rel in only_old:
        old_by_sha.setdefault(old[rel].get("sha256"), []).append(rel)

    matched_old: set = set()
    matched_new: set = set()
    for rel in sorted(only_new):
        sha256 = new[rel].get("sha256")
        candidates = old_by_sha.get(sha256) or []
        candidates = [c for c in candidates if c not in matched_old]
        if candidates:
            old_rel = candidates[0]
            matched_old.add(old_rel)
            matched_new.add(rel)
            diff.moved.append((old_rel, rel))

    diff.added = sorted(only_new - matched_new)
    diff.removed = sorted(only_old - matched_old)
    return diff


def scan_and_diff(local_dir: Path) -> StructureDiff:
    """Convenience wrapper: load the previous snapshot, build a fresh one,
    diff them, persist the fresh one for next time, return the diff.
    """
    previous = load_snapshot(local_dir)
    current = build_snapshot(local_dir)
    diff = diff_snapshots(previous, current)
    save_snapshot(local_dir, current)
    return diff


def siblings_by_rel(snapshot: Dict[str, dict]) -> Dict[str, List[str]]:
    """rel_path -> sorted filenames of everything else in the same local
    folder (per the snapshot's own rel paths, not a filesystem re-scan).
    Used to attach real folder context to an upload -- see module
    docstring point 2.
    """
    by_parent: Dict[str, List[str]] = {}
    for rel in snapshot:
        parent = str(Path(rel).parent) if Path(rel).parent != Path(".") else ""
        by_parent.setdefault(parent, []).append(Path(rel).name)

    result: Dict[str, List[str]] = {}
    for rel in snapshot:
        parent = str(Path(rel).parent) if Path(rel).parent != Path(".") else ""
        name = Path(rel).name
        result[rel] = sorted(n for n in by_parent.get(parent, []) if n != name)
    return result


# Local folder names that carry no real signal about project cohesion --
# generic system/download folders every machine has, in several languages.
# A file sitting directly in one of these (or at the sync root itself) gets
# no folder_hint at all rather than a misleading one.
_GENERIC_FOLDER_NAMES = {
    "desktop", "downloads", "download", "documents", "document", "temp", "tmp",
    "untitled", "new folder", "바탕화면", "다운로드", "문서", "새 폴더", "제목 없음",
}


def folder_hint_by_rel(snapshot: Dict[str, dict]) -> Dict[str, str]:
    """rel_path -> the file's immediate local parent folder's own name, when
    it looks like a real, user-chosen name rather than a generic system
    folder or the sync root itself (parent == ""). A human-picked folder
    name (e.g. "invoice_2024_project") is often the single best signal for
    what to call a project -- see api.upload_files' folder_hint_map /
    main.py's /api/files/upload `folder_hints` field / worker.py's
    _resolve_local_folder_hint, which still runs its own atomicity check
    before ever trusting this name for anything.
    """
    result: Dict[str, str] = {}
    for rel in snapshot:
        parent = Path(rel).parent
        if str(parent) in ("", "."):
            continue
        name = parent.name
        if name.strip().lower() in _GENERIC_FOLDER_NAMES:
            continue
        result[rel] = name
    return result
