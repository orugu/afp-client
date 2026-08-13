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

# Unsigned/unnotarized (no Apple Developer ID to sign with -- see the note
# at the top of this file), so Gatekeeper always blocks the first launch
# with "Apple could not verify ... is free of malware". Rather than making
# every user type the xattr command themselves, ship a double-clickable
# helper that does it for them: installs the app into /Applications if it
# isn't there yet, strips the quarantine flag, then opens it. Users can drag
# this file to their Desktop (or run it right out of the mounted dmg) and
# reuse it for every future update -- it's idempotent and always targets
# whatever .app already exists.
cat > "$STAGING_DIR/처음 실행하기.command" <<'HELPER_EOF'
#!/bin/bash
# FileSortingUploader -- 첫 실행 도우미
#
# 서명/공증되지 않은 앱이라 macOS Gatekeeper가 처음 여는 것을 막는데
# (브라우저로 받은 파일에 자동으로 붙는 com.apple.quarantine 표시 때문),
# 이 스크립트가 그 표시만 지우고 앱을 엽니다. 안의 코드를 바꾸는 게
# 아니라 macOS에 "이미 확인했다"고 알려주는 것뿐입니다.
#
# 한 번 실행해두면 그다음부터는 Dock/Launchpad에서 그냥 더블클릭하면
# 됩니다. 새 버전을 새로 받았을 때는 이 스크립트를 다시 실행해주세요
# (quarantine 표시는 다운로드할 때마다 새로 붙습니다).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="FileSortingUploader.app"
DEST="/Applications/$APP_NAME"

if [ ! -d "$DEST" ]; then
    if [ -d "$SCRIPT_DIR/$APP_NAME" ]; then
        echo "Applications 폴더에 설치하는 중..."
        cp -R "$SCRIPT_DIR/$APP_NAME" "$DEST"
    else
        osascript -e 'display alert "설치 실패" message "FileSortingUploader.app을 찾을 수 없습니다. 이 스크립트를 앱과 같은 폴더(다운로드한 dmg 안)에서 실행해주세요."'
        exit 1
    fi
fi

xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
open "$DEST"
HELPER_EOF
chmod +x "$STAGING_DIR/처음 실행하기.command"

hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH"

python3 "$CLIENT_ROOT/build/update_manifest.py" "$TARGET_DIR/uploader-manifest.json" "$APP_NAME.dmg"

echo "Built $DMG_PATH"
