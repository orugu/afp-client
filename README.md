# afp-client

Desktop/CLI client for [Auto File Processor](https://devlovers.cloud/file_managing) — watches a
local folder and auto-uploads new files for classification, and downloads
the sorted result back down. Windows/Linux/macOS.

## What's here

- `src/file_sorting_client/` — the Python package:
  - `cli.py` (`fsc`) — full CLI: `configure`, `status`, `upload`, `download`,
    `browse`, `search`, worker control, plus Windows-only WebDAV
    mount/sync commands (`windows mount/setup/...`).
  - `gui.py` (`fsc-gui`) — cross-platform Tkinter GUI wrapping the same
    upload-watcher/download-manager logic, no terminal needed.
  - `upload_watcher.py` / `download_manager.py` — the actual watch/upload
    and browse/download logic, both with a thread-pool for concurrent
    transfers; used by both `cli.py` and `gui.py`.
  - `windows/` — Windows-only rclone/WinFsp-based drive mount (`fsc-manager`
    / `FileSortingManager.exe`), predates and is independent of the
    cross-platform upload/download path above.
- `windows/`, `linux/`, `macos/` — per-OS install scripts and (gitignored)
  build outputs served to end users by the server.
- `build/` — PyInstaller build scripts, one per OS (`build_linux.sh`,
  `build_macos.sh`, `build_windows.ps1`) plus `README.md` explaining why
  three separate scripts exist. `.github/workflows/build-client.yml` runs
  all three on every push.

## Quickstart (CLI, any OS)

```bash
uv sync
uv run fsc configure --base-url https://devlovers.cloud/file_managing/api --token <API_TOKEN>
uv run fsc upload --watch ~/Downloads      # auto-upload new files, concurrently
uv run fsc download engineering --out ./local-copy   # recursive concurrent download
uv run fsc-gui                              # same thing, GUI
```

## Building a standalone binary

See [`build/README.md`](build/README.md) — short version: PyInstaller can't
cross-compile, each OS builds its own binary, CI (`.github/workflows/build-client.yml`)
does all three automatically on push.
