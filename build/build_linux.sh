#!/usr/bin/env bash
# Builds the onefile Linux binary of the cross-platform client GUI
# (file_sorting_client/gui.py) with PyInstaller. Must be run ON Linux --
# PyInstaller does not cross-compile (see build/README.md). Mirrors
# client/windows/build-manager.ps1's approach: uv-managed venv, no .spec
# file, driven entirely by CLI flags.
set -euo pipefail

CLIENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$CLIENT_ROOT/build/dist-linux"
TARGET_DIR="$CLIENT_ROOT/linux"

cd "$CLIENT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required (https://docs.astral.sh/uv/)." >&2
    exit 1
fi

uv sync
uv pip install pyinstaller

# --onefile: single binary, no separate lib folder to ship/chmod.
# No --windowed on Linux (that flag is macOS/Windows-only in PyInstaller);
# Tkinter's own window is all that shows regardless.
uv run pyinstaller \
    --noconfirm \
    --onefile \
    --name FileSortingUploader \
    --distpath "$OUT_DIR" \
    src/file_sorting_client/gui.py

mkdir -p "$TARGET_DIR"
cp "$OUT_DIR/FileSortingUploader" "$TARGET_DIR/FileSortingUploader"
chmod +x "$TARGET_DIR/FileSortingUploader"

python3 "$CLIENT_ROOT/build/update_manifest.py" "$TARGET_DIR/manifest.json" FileSortingUploader

echo "Built $TARGET_DIR/FileSortingUploader"
