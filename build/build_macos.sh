#!/usr/bin/env bash
# Builds a macOS .dmg of the cross-platform client GUI
# (file_sorting_client/gui.py) with PyInstaller. Must be run ON macOS --
# PyInstaller does not cross-compile (see build/README.md).
#
# NOTE: macOS Gatekeeper will flag an unsigned/unnotarized app ("Apple
# could not verify ... is free of malware"). Without an Apple Developer ID
# to codesign + notarize with, users have to clear the quarantine flag
# manually the first time -- see build/README.md for the exact steps. This
# script does not attempt signing/notarization -- there's no certificate
# configured for this project to sign with.
set -euo pipefail

CLIENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$CLIENT_ROOT/build/dist-macos"
TARGET_DIR="$CLIENT_ROOT/macos"
APP_NAME="FileSortingUploader"

cd "$CLIENT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required (https://docs.astral.sh/uv/)." >&2
    exit 1
fi

uv sync
uv pip install pyinstaller

# --windowed on macOS makes PyInstaller emit a proper .app bundle (not just
# a bare Mach-O binary) at $OUT_DIR/$APP_NAME.app -- that bundle, not the
# raw executable inside it, is what Finder needs to recognize this as an
# application. Shipping the bare binary directly (an earlier version of
# this script did) makes Finder try to open it as a document instead of
# running it -- e.g. "cannot be opened ... Unicode (UTF-8) text encoding".
uv run pyinstaller \
    --noconfirm \
    --onefile \
    --windowed \
    --name "$APP_NAME" \
    --distpath "$OUT_DIR" \
    src/file_sorting_client/gui.py

APP_BUNDLE="$OUT_DIR/$APP_NAME.app"
if [ ! -d "$APP_BUNDLE" ]; then
    echo "Expected PyInstaller to produce $APP_BUNDLE (--windowed on macOS should always emit a .app bundle) but it's missing." >&2
    exit 1
fi

mkdir -p "$TARGET_DIR"
DMG_PATH="$TARGET_DIR/$APP_NAME.dmg"
rm -f "$DMG_PATH"

# Standard "drag the .app onto an /Applications shortcut" dmg layout.
STAGING_DIR="$OUT_DIR/dmg-staging"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
cp -R "$APP_BUNDLE" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH"

python3 "$CLIENT_ROOT/build/update_manifest.py" "$TARGET_DIR/manifest.json" "$APP_NAME.dmg"

echo "Built $DMG_PATH"
