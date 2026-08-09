"""Self-update check (and, where it's safe to do unattended, self-replace)
against the per-OS manifest.json the server already publishes at
{app_base_url}/downloads/{os}/manifest.json (same file build/*.sh writes,
see client/build/update_manifest.py).

Scope, deliberately: full update-*checking* everywhere (cheap, read-only,
safe to get wrong). Actual in-place self-replacement is only attempted on
Linux, where POSIX semantics make it safe (replacing the file a running
process was exec'd from doesn't affect that already-running process -- the
old inode just keeps existing until the process exits). Windows can't
overwrite its own running .exe, and macOS ships as a .dmg/.app bundle where
in-place replacement is a lot more involved than swapping one file; for
both, this only reports that an update exists and hands back a URL to open
rather than attempting anything unverified. Better to under-promise here
than ship self-modifying-binary code for platforms this couldn't be tested
on.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from file_sorting_client import __version__

_OS_NAMES = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
_BINARY_NAMES = {
    "linux": "FileSortingUploader",
    "macos": "FileSortingUploader.dmg",
    "windows": "FileSortingUploader.exe",
}


def current_os() -> str:
    return _OS_NAMES.get(platform.system(), "linux")


def _parse_version(v: str) -> tuple:
    parts = []
    for chunk in v.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@dataclass
class UpdateInfo:
    current_version: str
    latest_version: str
    download_url: str
    os_name: str

    @property
    def can_self_apply(self) -> bool:
        # See module docstring: only Linux gets the in-place replace path.
        return self.os_name == "linux" and getattr(sys, "frozen", False)


def check_for_update(base_url: str, timeout: float = 10.0) -> Optional[UpdateInfo]:
    """`base_url` accepts either the app base (".../file_managing") or the
    API base (".../file_managing/api") -- callers usually have whichever
    one ClientSettings already carries, and getting this wrong would
    otherwise silently 404 into "no update available" instead of erroring
    loudly, which isn't a helpful failure mode to hand callers.

    Returns None if already current, unreachable, or the manifest is
    malformed -- callers should treat "no update info" as "nothing to do",
    not as an error worth surfacing to the user."""
    os_name = current_os()
    app_base_url = base_url.rstrip("/")
    if app_base_url.endswith("/api"):
        app_base_url = app_base_url[: -len("/api")]
    # A dedicated filename, not the pre-existing manifest.json -- on
    # Windows that file already tracks a different, independently-versioned
    # thing (the rclone/WinFsp install-pipeline scripts, see
    # windows/manifest.json + update-client.ps1). Reusing it here would
    # mean this app's version bumps get entangled with that pipeline's, or
    # worse, checking a "files" list that was never told this app's binary
    # exists and permanently reporting "no update" for Windows.
    manifest_url = f"{app_base_url}/downloads/{os_name}/uploader-manifest.json"
    try:
        response = httpx.get(manifest_url, timeout=timeout)
        response.raise_for_status()
        manifest = response.json()
    except Exception:
        return None

    latest_version = str(manifest.get("version", "")).strip()
    if not latest_version or _parse_version(latest_version) <= _parse_version(__version__):
        return None

    binary_name = _BINARY_NAMES.get(os_name, "FileSortingUploader")
    if binary_name not in manifest.get("files", []):
        return None

    download_url = f"{app_base_url.rstrip('/')}/downloads/{os_name}/{binary_name}"
    return UpdateInfo(
        current_version=__version__,
        latest_version=latest_version,
        download_url=download_url,
        os_name=os_name,
    )


def apply_update_linux(info: UpdateInfo, timeout: float = 60.0) -> None:
    """Downloads the new binary, atomically replaces the currently-running
    executable, then relaunches it and exits this process. Only call this
    when info.can_self_apply is True.

    Safe because of a POSIX guarantee: os.replace() unlinks the old
    directory entry and creates a new one; the process currently executing
    from the old inode keeps running from it untouched (the kernel doesn't
    care that the path now points somewhere else) until *this* process
    exits and the new one starts from the new file.
    """
    if not info.can_self_apply:
        raise RuntimeError("apply_update_linux called on a non-self-updatable build")

    current_path = Path(sys.executable).resolve()
    response = httpx.get(info.download_url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()

    fd, tmp_path = tempfile.mkstemp(dir=str(current_path.parent), prefix=".update-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(response.content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, current_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    subprocess.Popen([str(current_path)], start_new_session=True)
    os._exit(0)
