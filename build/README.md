# Client build pipeline

`file_sorting_client/gui.py` is the cross-platform desktop client (upload
watcher + download manager, Tkinter). It builds into a single native binary
per OS with PyInstaller:

| OS      | Script                     | Output                        |
|---------|-----------------------------|--------------------------------|
| Linux   | `build_linux.sh`            | `linux/FileSortingUploader` |
| macOS   | `build_macos.sh`            | `macos/FileSortingUploader.dmg` |
| Windows | `build_windows.ps1`         | `windows/FileSortingUploader.exe` |

macOS ships as a `.dmg` (not a bare binary): PyInstaller's `--windowed` flag
produces a real `.app` bundle on macOS, and Finder needs that bundle -- not
the raw executable inside it -- to recognize the download as an
application. A bare binary double-clicked in Finder gets treated as an
unrecognized document instead (surfaces as a "Unicode (UTF-8) text encoding"
error, since Finder tries to open it as text). The `.dmg` mounts and offers
the standard drag-the-.app-onto-Applications flow.

This repo (`orugu/afp-client`) is the client only -- the server side
(`auto-file-processor`) lives in a separate, non-public project and serves
these built binaries from its own disk (mounted independently of this repo's
git history). Pushing here only affects source + CI; it doesn't touch the
running server, and the server needs the binaries copied over manually or
via the workflow's uploaded artifacts.

## Why three separate scripts, not one

**PyInstaller cannot cross-compile.** It bundles the interpreter and native
extension modules (including Tcl/Tk) it finds on the machine it runs on, so
a Linux build produces only a Linux binary, a macOS build only a macOS
binary, and so on — there is no `--target-os` flag. Each script must
actually run on its target OS.

This means the Linux binary can be (and has been) built directly in this
project's own environment. The macOS and Windows binaries cannot be
produced here — there is no Mac or Windows machine available in this
sandbox — and must be built by running `build_macos.sh` / `build_windows.ps1`
on an actual Mac / Windows machine (or a CI runner of that OS — see below).

## Recommended: GitHub Actions matrix build

`.github/workflows/build-client.yml` builds all three OSes in parallel on
every push (using `windows-latest` / `macos-latest` / `ubuntu-latest`
runners, which is exactly how open-source PyInstaller projects normally
solve this) and uploads each binary as a workflow artifact. Runs
automatically on every push to this repo -- no further setup needed.

## Manual build (no CI)

```bash
# On Linux:
./build/build_linux.sh

# On macOS:
./build/build_macos.sh

# On Windows (PowerShell):
.\build\build_windows.ps1
```

Each script also updates that OS's `manifest.json` (version + file list),
the same manifest format `windows/manifest.json` already uses.

## Known limitations

- **macOS Gatekeeper**: the built binary is unsigned (no Apple Developer ID
  configured for this project). Users will see "Apple could not verify
  ... is free of malware" the first time. On current macOS (Sequoia+) the
  classic right-click → Open trick doesn't reliably surface an Open option
  for this exact dialog anymore -- the two workarounds that do work:
  - `xattr -d com.apple.quarantine /Applications/FileSortingUploader.app`
    (after dragging it there from the mounted .dmg), then reopen, or
  - System Settings → Privacy & Security → scroll to the bottom → "Open
    Anyway" next to the blocked-app notice.
  Actual codesigning + notarization needs an Apple Developer account's
  certificate, which isn't available here.
- **Linux**: no distro packaging (no `.deb`/`.rpm`/AppImage) — just a bare
  onefile ELF binary. `chmod +x` and run.
- **Windows**: unsigned `.exe` will likely trigger a SmartScreen warning on
  first run, same root cause as the macOS Gatekeeper note above.
