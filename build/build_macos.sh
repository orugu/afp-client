#!/usr/bin/env bash
# Builds the onefile macOS binary of the cross-platform client GUI
# (file_sorting_client/gui.py) with PyInstaller. Must be run ON macOS --
# PyInstaller does not cross-compile (see build/README.md).
#
# NOTE: macOS Gatekeeper will flag an unsigned/unnotarized binary
# ("cannot be opened because the developer cannot be verified"). Without an
# Apple Developer ID to codesign + notarize with, users have to right-click
# -> Open the first time, or run `xattr -d com.apple.quarantine
# FileSortingUploader` after downloading. This script does not attempt
# signing/notarization -- there's no certificate configured for this
# project to sign with.
set -euo pipefail

CLIENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$CLIENT_ROOT/build/dist-macos"
TARGET_DIR="$CLIENT_ROOT/macos"

cd "$CLIENT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required (https://docs.astral.sh/uv/)." >&2
    exit 1
fi

uv sync
uv pip install pyinstaller

# --windowed on macOS avoids a Terminal window popping up alongside the Tk
# window when double-clicked from Finder.
uv run pyinstaller \
    --noconfirm \
    --onefile \
    --windowed \
    --name FileSortingUploader \
    --distpath "$OUT_DIR" \
    src/file_sorting_client/gui.py

mkdir -p "$TARGET_DIR"
cp "$OUT_DIR/FileSortingUploader" "$TARGET_DIR/FileSortingUploader"
chmod +x "$TARGET_DIR/FileSortingUploader"

python3 "$CLIENT_ROOT/build/update_manifest.py" "$TARGET_DIR/manifest.json" FileSortingUploader

echo "Built $TARGET_DIR/FileSortingUploader"
