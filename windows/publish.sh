#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/client/windows"
DEST="$ROOT/app_source/exe"

mkdir -p "$DEST"
rsync -a --delete \
  --exclude 'build/' \
  --exclude 'dist/' \
  --exclude '*.exe' \
  "$SRC/" "$DEST/"

echo "Published Windows downloads to $DEST"
echo "Files:"
find "$DEST" -maxdepth 2 -type f | sort
