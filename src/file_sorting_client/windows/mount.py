from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


PROGRAM_DATA_ROOT = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "FileSortingClient"
CACHE_DIR = PROGRAM_DATA_ROOT / "cache"
CONFIG_DIR = PROGRAM_DATA_ROOT / "config"
SCRIPTS_DIR = PROGRAM_DATA_ROOT / "scripts"
STATE_FILE = PROGRAM_DATA_ROOT / "state.json"
MOUNT_LOG = PROGRAM_DATA_ROOT / "mount.log"
INSTALL_LOG = PROGRAM_DATA_ROOT / "install.log"
MOUNT_PID_FILE = PROGRAM_DATA_ROOT / "mount.pid"


@dataclass(frozen=True)
class ClientState:
    app_base_url: str
    api_base_url: str
    api_token: str
    dav_url: str
    remote_name: str
    drive_letter: str
    webdav_username: str
    installed_at: str
    version: str
    webdav_password: str = ""

    @classmethod
    def load(cls) -> "ClientState | None":
        if not STATE_FILE.exists():
            return None
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return cls(**data)

    def save(self) -> None:
        PROGRAM_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8")


def is_windows() -> bool:
    return platform.system() == "Windows"


def normalize_dav_url(url: str) -> str:
    return f"{url.rstrip('/')}/"


def ensure_directories() -> None:
    for path in (PROGRAM_DATA_ROOT, CACHE_DIR, CONFIG_DIR, SCRIPTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def find_rclone() -> Path:
    found = shutil.which("rclone")
    if found:
        return Path(found)

    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "rclone" / "rclone.exe",
        Path(os.environ.get("LocalAppData", "")) / "Microsoft" / "WinGet" / "Links" / "rclone.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "rclone" / "rclone.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("rclone not found. Run install.ps1 first.")


def drive_is_mounted(drive_letter: str) -> bool:
    root = f"{drive_letter.strip(':').upper()}:\\"
    return os.path.exists(root)


def get_mount_log_tail(lines: int = 20) -> str:
    if not MOUNT_LOG.exists():
        return "No mount log yet."
    content = MOUNT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def obscure_password(rclone: Path, password: str) -> str:
    result = subprocess.run(
        [str(rclone), "obscure", password],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()[-1]


def get_mount_processes(drive_letter: str) -> list[int]:
    if not is_windows():
        return []

    letter = drive_letter.strip(":").upper()
    pattern = f"rclone.*mount.*{letter}:"
    try:
        output = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    f"Get-CimInstance Win32_Process | "
                    f"Where-Object {{ $_.CommandLine -and ($_.CommandLine -match '{pattern}') }} | "
                    f"Select-Object -ExpandProperty ProcessId"
                ),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []

    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_mount(drive_letter: str) -> None:
    letter = drive_letter.strip(":").upper()
    for pid in get_mount_processes(letter):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if MOUNT_PID_FILE.exists():
        MOUNT_PID_FILE.unlink(missing_ok=True)
    subprocess.run(
        ["cmd.exe", "/c", f"net use {letter}: /delete /y"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def configure_rclone_remote(
    *,
    remote_name: str,
    dav_url: str,
    username: str,
    password: str,
) -> None:
    rclone = find_rclone()
    pass_obscured = obscure_password(rclone, password)
    subprocess.run(
        [str(rclone), "config", "delete", remote_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            str(rclone),
            "config",
            "create",
            remote_name,
            "webdav",
            "url",
            normalize_dav_url(dav_url),
            "vendor",
            "other",
            "user",
            username,
            "pass",
            pass_obscured,
            "--non-interactive",
        ],
        check=True,
    )
    test_remote(remote_name)


def test_remote(remote_name: str) -> None:
    rclone = find_rclone()
    subprocess.run([str(rclone), "lsd", f"{remote_name}:"], check=True)


def start_mount(
    *,
    remote_name: str,
    drive_letter: str,
    cache_dir: Path | None = None,
) -> None:
    if not is_windows():
        raise RuntimeError("Mount is only supported on Windows.")

    ensure_directories()
    letter = drive_letter.strip(":").upper()
    if drive_is_mounted(letter):
        return

    stop_mount(letter)
    test_remote(remote_name)
    rclone = find_rclone()
    cache_path = cache_dir or CACHE_DIR
    cache_path.mkdir(parents=True, exist_ok=True)
    if MOUNT_LOG.exists():
        MOUNT_LOG.unlink(missing_ok=True)

    args = [
        str(rclone),
        "mount",
        f"{remote_name}:",
        f"{letter}:",
        "--vfs-cache-mode",
        "full",
        "--vfs-cache-max-age",
        "1h",
        "--vfs-write-back",
        "10s",
        "--vfs-read-chunk-size",
        "32M",
        "--vfs-read-chunk-size-limit",
        "256M",
        "--dir-cache-time",
        "30s",
        "--poll-interval",
        "15s",
        "--cache-dir",
        str(cache_path),
        "--volname",
        "File Sorting",
        "--links",
        "--timeout",
        "60s",
        "--attr-timeout",
        "30s",
        "--log-file",
        str(MOUNT_LOG),
        "--log-level",
        "INFO",
    ]

    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    MOUNT_PID_FILE.write_text(str(process.pid), encoding="ascii")

    deadline = time.time() + 45
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"rclone mount exited early (code={process.returncode}).\n{get_mount_log_tail()}"
            )
        if drive_is_mounted(letter):
            return
        time.sleep(0.7)
    raise TimeoutError(f"Mount did not become ready within 45 seconds.\n{get_mount_log_tail()}")


def start_webdav_net_use(
    *,
    dav_url: str,
    username: str,
    password: str,
    drive_letter: str,
) -> None:
    letter = drive_letter.strip(":").upper()
    stop_mount(letter)
    result = subprocess.run(
        [
            "cmd.exe",
            "/c",
            f'net use {letter}: "{normalize_dav_url(dav_url)}" /user:{username} {password} /persistent:yes',
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not drive_is_mounted(letter):
        raise RuntimeError(result.stderr.strip() or "Windows WebDAV mapping failed")


def cleanup_cache(*, cache_dir: Path | None = None, max_age_hours: int = 1) -> int:
    target = cache_dir or CACHE_DIR
    if not target.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_atime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def mount_status(drive_letter: str) -> dict[str, object]:
    letter = drive_letter.strip(":").upper()
    pids = get_mount_processes(letter)
    return {
        "drive_letter": letter,
        "mounted": drive_is_mounted(letter),
        "process_ids": pids,
        "cache_dir": str(CACHE_DIR),
        "mount_log": str(MOUNT_LOG),
        "mount_log_tail": get_mount_log_tail(),
    }
