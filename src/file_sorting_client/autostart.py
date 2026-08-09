"""Register/unregister this app to launch automatically on login, per OS.
Needed for the "runs in the background, always in sync" behavior -- without
this, the sync loop only runs while the user remembers to open the app.

- Windows: HKCU Run registry key (no admin rights needed, per-user).
- Linux: XDG autostart .desktop file (~/.config/autostart/), respected by
  GNOME/KDE/most desktop environments.
- macOS: a launchd user LaunchAgent .plist (~/Library/LaunchAgents/),
  loaded immediately via `launchctl load` in addition to being picked up on
  next login.

Each platform's functions are only imported/called when that OS matches --
e.g. `winreg` doesn't exist on Linux/macOS, so it's imported lazily inside
the Windows-only functions rather than at module load time.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

APP_NAME = "FileSortingUploader"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _executable_path() -> Path:
    """Path to the running binary. Only meaningful when frozen (PyInstaller
    build) -- registering a `python -m file_sorting_client.gui` invocation
    to autostart would break the moment the dev's checkout moves, so
    autostart is a no-op in that case (see is_supported())."""
    return Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()


def is_supported() -> bool:
    """False when running from source (not a frozen PyInstaller build) --
    nothing sensible to point autostart at in that case."""
    return bool(getattr(sys, "frozen", False))


# ── Windows ──────────────────────────────────────────────────────────────

def _windows_enable() -> None:
    import winreg  # type: ignore[import-not-found]

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{_executable_path()}"')
    finally:
        winreg.CloseKey(key)


def _windows_disable() -> None:
    import winreg  # type: ignore[import-not-found]

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    try:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
    finally:
        winreg.CloseKey(key)


def _windows_is_enabled() -> bool:
    import winreg  # type: ignore[import-not-found]

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except (FileNotFoundError, OSError):
        return False


# ── Linux (XDG autostart) ───────────────────────────────────────────────

def _linux_desktop_file() -> Path:
    return Path.home() / ".config" / "autostart" / "file-sorting-uploader.desktop"


def _linux_enable() -> None:
    path = _linux_desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f'Exec="{_executable_path()}"\n'
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n",
        encoding="utf-8",
    )


def _linux_disable() -> None:
    _linux_desktop_file().unlink(missing_ok=True)


def _linux_is_enabled() -> bool:
    return _linux_desktop_file().exists()


# ── macOS (launchd LaunchAgent) ─────────────────────────────────────────

_MACOS_LABEL = "cloud.devlovers.filesortinguploader"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_LABEL}.plist"


def _macos_enable() -> None:
    path = _macos_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = _executable_path()
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>\n"
        f"  <key>Label</key><string>{_MACOS_LABEL}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>{exe}</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "</dict></plist>\n",
        encoding="utf-8",
    )
    subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True)


def _macos_disable() -> None:
    path = _macos_plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink(missing_ok=True)


def _macos_is_enabled() -> bool:
    return _macos_plist_path().exists()


# ── Public API ───────────────────────────────────────────────────────────

def enable() -> None:
    if _is_windows():
        _windows_enable()
    elif _is_macos():
        _macos_enable()
    else:
        _linux_enable()


def disable() -> None:
    if _is_windows():
        _windows_disable()
    elif _is_macos():
        _macos_disable()
    else:
        _linux_disable()


def is_enabled() -> bool:
    if _is_windows():
        return _windows_is_enabled()
    if _is_macos():
        return _macos_is_enabled()
    return _linux_is_enabled()
