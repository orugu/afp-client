#!/usr/bin/env python3
"""Shared helper for build_linux.sh / build_macos.sh / build_windows.ps1:
creates or updates a downloads manifest.json ({"version", "files"}) next to
a freshly built binary, the same shape client/windows/manifest.json already
uses. Kept as one small script instead of duplicating this logic in bash
and PowerShell.

Version always comes from file_sorting_client.__version__ (not a CLI arg)
so the manifest can never drift out of sync with what got built -- this is
also exactly the version updater.py compares against, so a stale manifest
version would silently break the self-update check.

Usage: update_manifest.py <manifest_path> <binary_filename>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from file_sorting_client import __version__  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(1)

    manifest_path = Path(sys.argv[1])
    binary_name = sys.argv[2]

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"version": __version__, "files": []}

    manifest["version"] = __version__
    if binary_name not in manifest.setdefault("files", []):
        manifest["files"].append(binary_name)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {manifest_path} (version {__version__})")


if __name__ == "__main__":
    main()
