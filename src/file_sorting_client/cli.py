from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich.console import Console

from file_sorting_client.api import ApiError, FileSortingApiClient
from file_sorting_client.config import ClientSettings
from file_sorting_client.display import (
    print_bootstrap,
    print_connectivity,
    print_decisions_table,
    print_download_validation,
    print_health,
    print_json,
    print_worker_status,
)
from file_sorting_client import __version__, autostart
from file_sorting_client.download_manager import download_tree
from file_sorting_client.sync_manager import SyncLoop, run_sync
from file_sorting_client.updater import apply_update_linux, check_for_update
from file_sorting_client.upload_watcher import UploadWatcher

app = typer.Typer(
    name="fsc",
    help="Auto File Processor API client",
    no_args_is_help=True,
)
worker_app = typer.Typer(help="Worker control commands")
windows_app = typer.Typer(help="Windows sync client commands")
credentials_app = typer.Typer(help="WebDAV credential commands")
downloads_app = typer.Typer(help="Download manifest commands")
mount_app = typer.Typer(help="Local drive mount commands (Windows only)")
autostart_app = typer.Typer(help="Launch-on-login commands")

app.add_typer(worker_app, name="worker")
app.add_typer(windows_app, name="windows")
app.add_typer(autostart_app, name="autostart")
windows_app.add_typer(credentials_app, name="credentials")
windows_app.add_typer(downloads_app, name="downloads")
windows_app.add_typer(mount_app, name="mount")

console = Console()


def _resolve_settings(
    base_url: Optional[str],
    token: Optional[str],
    config_file: Optional[Path],
) -> ClientSettings:
    settings = ClientSettings.load(config_file)
    return settings.with_overrides(api_base_url=base_url, api_token=token)


def _client(
    base_url: Optional[str],
    token: Optional[str],
    config_file: Optional[Path],
) -> FileSortingApiClient:
    settings = _resolve_settings(base_url, token, config_file)
    if not settings.api_token:
        console.print(
            "[yellow]API token is not configured.[/yellow] "
            "Run [bold]fsc configure[/bold] or pass [bold]--token[/bold]."
        )
        raise typer.Exit(code=1)
    return FileSortingApiClient(settings)


def _handle_api_error(exc: ApiError) -> None:
    if exc.status_code == 401:
        console.print("[red]Invalid or missing API token.[/red]")
    else:
        console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1) from exc


CommonBaseUrl = Annotated[
    Optional[str],
    typer.Option("--base-url", help="API base URL (default: from config or env)"),
]
CommonToken = Annotated[
    Optional[str],
    typer.Option("--token", help="Bearer token (default: from config or env)"),
]
CommonConfig = Annotated[
    Optional[Path],
    typer.Option("--config", help="Path to client settings file"),
]
CommonJson = Annotated[
    bool,
    typer.Option("--json", help="Print raw JSON response"),
]


@app.command("configure")
def configure(
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", prompt="API base URL", help="API base URL"),
    ] = None,
    token: Annotated[
        Optional[str],
        typer.Option("--token", prompt=True, hide_input=True, help="Bearer token"),
    ] = None,
    config_file: CommonConfig = None,
) -> None:
    """Save API base URL and token to the local config file."""
    settings = ClientSettings.load(config_file)
    if base_url is not None:
        settings = settings.with_overrides(api_base_url=base_url)
    if token is not None:
        settings = settings.with_overrides(api_token=token)
    settings.save()
    console.print(f"[green]Saved settings to[/green] {settings.config_file}")


@app.command("show-config")
def show_config(config_file: CommonConfig = None) -> None:
    """Show the current client configuration."""
    settings = ClientSettings.load(config_file)
    print_json(
        {
            "config_file": str(settings.config_file),
            "api_base_url": settings.api_base_url,
            "api_token_set": bool(settings.api_token),
        }
    )


@app.command("health")
def health(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Check API health (no auth required)."""
    settings = _resolve_settings(base_url, token, config_file)
    try:
        with FileSortingApiClient(settings) as client:
            result = client.health()
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_health(result.ok, result.worker_running)


@app.command("status")
def status(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Show worker status and recent decisions."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.status()
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_worker_status(result)


@app.command("config")
def server_config(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Show server-side configuration."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.config()
    except ApiError as exc:
        _handle_api_error(exc)

    print_json(result.model_dump())


@worker_app.command("start")
def worker_start(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Start the file worker."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.start_worker()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@worker_app.command("stop")
def worker_stop(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Stop the file worker."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.stop_worker()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@worker_app.command("scan-once")
def worker_scan_once(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Run a single scan pass."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.scan_once()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@app.command("search")
def search(
    q: Annotated[Optional[str], typer.Option("--q", help="Search query")] = None,
    category: Annotated[Optional[str], typer.Option("--category", help="Category filter")] = None,
    status_filter: Annotated[
        Optional[str],
        typer.Option("--status", help="Status filter (moved/failed)"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Search file decision history."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.search(q=q, category=category, status=status_filter, limit=limit)
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_decisions_table(result.items)


@app.command("upload")
def upload(
    paths: Annotated[
        Optional[List[Path]],
        typer.Argument(help="Specific file(s) to upload once, no watching"),
    ] = None,
    watch_dir: Annotated[
        Optional[Path],
        typer.Option("--watch", help="Folder to watch continuously; new/changed files upload automatically"),
    ] = None,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=32, help="Concurrent uploads in flight")
    ] = 4,
    interval: Annotated[
        float, typer.Option("--interval", min=1.0, help="Watch-mode poll interval in seconds")
    ] = 3.0,
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Upload files to the server, either once (positional PATHS) or
    continuously (--watch DIR uploads every new/changed file it sees,
    concurrently, until Ctrl+C)."""
    if not paths and not watch_dir:
        console.print("[yellow]Pass file(s) to upload, or --watch DIR to watch a folder continuously.[/yellow]")
        raise typer.Exit(code=1)

    with _client(base_url, token, config_file) as client:
        if paths:
            missing = [p for p in paths if not p.is_file()]
            if missing:
                console.print(f"[red]Not a file: {', '.join(str(p) for p in missing)}[/red]")
                raise typer.Exit(code=1)
            try:
                result = client.upload_files(list(paths))
            except ApiError as exc:
                _handle_api_error(exc)
            console.print(f"[green]Uploaded {len(result.saved)} file(s).[/green]")
            for name in result.saved:
                console.print(f"- {name}")
            if result.skipped:
                console.print(f"[yellow]Skipped {len(result.skipped)}:[/yellow] {', '.join(result.skipped)}")

        if watch_dir:
            watch_dir.mkdir(parents=True, exist_ok=True)
            console.print(
                f"[green]Watching[/green] {watch_dir.resolve()} "
                f"[dim](concurrency={concurrency}, every {interval}s, Ctrl+C to stop)[/dim]"
            )

            def _on_event(event) -> None:
                if event.ok:
                    console.print(f"[green]uploaded[/green] {event.path.name}")
                else:
                    console.print(f"[red]failed[/red] {event.path.name}: {event.message}")

            watcher = UploadWatcher(
                client=client,
                watch_dir=watch_dir,
                concurrency=concurrency,
                poll_interval_seconds=interval,
                on_event=_on_event,
            )
            try:
                watcher.run_forever()
            except KeyboardInterrupt:
                console.print("\n[dim]Stopped watching.[/dim]")


@app.command("browse")
def browse(
    path: Annotated[str, typer.Argument(help="Folder path relative to the organized output root")] = "",
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """List organized files/folders on the server."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.browse(path)
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
        return

    console.print(f"[bold]{result.path or '/'}[/bold]")
    for entry in result.entries:
        marker = "📁" if entry.type == "dir" else "📄"
        size = f" ({entry.size} bytes)" if entry.size is not None else ""
        console.print(f"{marker} {entry.name}{size}")


@app.command("download")
def download(
    remote_path: Annotated[str, typer.Argument(help="File or folder path on the server to download")],
    output_dir: Annotated[
        Path, typer.Option("--out", help="Local directory to save into")
    ] = Path("downloads"),
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=32, help="Concurrent downloads in flight")
    ] = 4,
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Download a single organized file, or an entire folder recursively,
    with up to --concurrency transfers in flight at once."""

    def _on_event(event) -> None:
        if event.ok:
            console.print(f"[green]downloaded[/green] {event.remote_path}")
        else:
            console.print(f"[red]failed[/red] {event.remote_path}: {event.message}")

    try:
        with _client(base_url, token, config_file) as client:
            events = download_tree(
                client, remote_path, output_dir, concurrency=concurrency, on_event=_on_event
            )
    except ApiError as exc:
        _handle_api_error(exc)

    ok = sum(1 for e in events if e.ok)
    failed = len(events) - ok
    console.print(f"[green]{ok} downloaded[/green]" + (f", [red]{failed} failed[/red]" if failed else ""))
    console.print(f"Saved to {output_dir.resolve()}")


@app.command("sync")
def sync(
    local_dir: Annotated[Path, typer.Argument(help="Local folder to keep in sync with the server")],
    remote_path: Annotated[
        str, typer.Option("--remote", help="Server-side folder to sync against (default: everything)")
    ] = "",
    watch: Annotated[
        bool, typer.Option("--watch", help="Keep syncing in the background instead of running once")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", min=1.0, help="--watch poll interval in seconds")
    ] = 15.0,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=32, help="Concurrent transfers in flight")
    ] = 4,
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Reconcile a local folder against the server: local-only files get
    uploaded, server-only files (from anyone -- this is what makes a synced
    folder behave like a shared Nextcloud-style drive rather than a
    personal upload queue) get downloaded. Never deletes or overwrites --
    same relative path with a different size is reported and left alone."""
    local_dir.mkdir(parents=True, exist_ok=True)

    def _on_event(event) -> None:
        marker = "[green]✓[/green]" if event.ok else "[yellow]![/yellow]"
        console.print(f"{marker} [{event.kind}] {event.label} {event.message}")

    try:
        with _client(base_url, token, config_file) as client:
            if not watch:
                events = run_sync(client, local_dir, remote_path, concurrency=concurrency, on_event=_on_event)
                ok = sum(1 for e in events if e.ok)
                console.print(f"[green]{ok}/{len(events)} synced[/green]")
                return

            console.print(f"[green]Watching[/green] {local_dir.resolve()} [dim](Ctrl+C to stop)[/dim]")
            loop = SyncLoop(
                client=client,
                local_dir=local_dir,
                remote_path=remote_path,
                concurrency=concurrency,
                poll_interval_seconds=interval,
                on_event=_on_event,
            )
            try:
                loop.run_forever()
            except KeyboardInterrupt:
                console.print("\n[dim]Stopped syncing.[/dim]")
    except ApiError as exc:
        _handle_api_error(exc)


@app.command("update")
def update(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the update if one is found (Linux builds only)")] = False,
) -> None:
    """Check the server's manifest for a newer client version."""
    settings = _resolve_settings(base_url, token, config_file)
    info = check_for_update(settings.api_base_url)
    if not info:
        console.print(f"[green]Up to date[/green] (v{__version__})")
        return

    console.print(f"[yellow]Update available:[/yellow] v{info.current_version} -> v{info.latest_version}")
    if not apply:
        console.print(f"Download: {info.download_url}")
        console.print("Re-run with --apply to install now." if info.can_self_apply else "Download and reinstall manually.")
        return

    if not info.can_self_apply:
        console.print(f"[yellow]Self-update isn't supported on this platform.[/yellow] Download: {info.download_url}")
        raise typer.Exit(code=1)

    console.print("Applying update and restarting...")
    apply_update_linux(info)


@autostart_app.command("enable")
def autostart_enable() -> None:
    """Register this app to launch automatically on login."""
    if not autostart.is_supported():
        console.print("[yellow]Only supported when running a built executable, not from source.[/yellow]")
        raise typer.Exit(code=1)
    autostart.enable()
    console.print("[green]Enabled.[/green]")


@autostart_app.command("disable")
def autostart_disable() -> None:
    """Unregister launch-on-login."""
    autostart.disable()
    console.print("[green]Disabled.[/green]")


@autostart_app.command("status")
def autostart_status() -> None:
    """Show whether launch-on-login is currently enabled."""
    if not autostart.is_supported():
        console.print("[dim]Not supported (running from source).[/dim]")
        return
    console.print("Enabled" if autostart.is_enabled() else "Disabled")


@app.command("watch")
def watch(
    interval: Annotated[float, typer.Option("--interval", min=1.0, help="Poll interval in seconds")] = 5.0,
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Poll worker status until interrupted."""
    try:
        with _client(base_url, token, config_file) as client:
            while True:
                console.clear()
                result = client.status()
                print_worker_status(result)
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped watching.[/dim]")
    except ApiError as exc:
        _handle_api_error(exc)


@windows_app.command("bootstrap")
def windows_bootstrap(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Show Windows client bootstrap URLs and settings."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_bootstrap()
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_bootstrap(result)


@windows_app.command("connectivity")
def windows_connectivity(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Check manifest and WebDAV connectivity."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_connectivity()
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_connectivity(result)


@credentials_app.command("show")
def credentials_show(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Show current WebDAV credentials."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_credentials()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@credentials_app.command("set")
def credentials_set(
    username: Annotated[str, typer.Option("--username", prompt=True)],
    password: Annotated[str, typer.Option("--password", prompt=True, hide_input=True)],
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Update WebDAV credentials on the server."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_set_credentials(username, password)
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@credentials_app.command("reset")
def credentials_reset(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Reset WebDAV credentials to server env defaults."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_reset_credentials()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@downloads_app.command("manifest")
def downloads_manifest(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Fetch and show the deployment manifest."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_manifest()
    except ApiError as exc:
        _handle_api_error(exc)
    print_json(result.model_dump())


@downloads_app.command("validate")
def downloads_validate(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Validate manifest and downloadable files."""
    try:
        with _client(base_url, token, config_file) as client:
            result = client.windows_validate_downloads()
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(result.model_dump())
    else:
        print_download_validation(result)


@windows_app.command("fetch-installer")
def fetch_installer(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Directory to save installer files"),
    ] = Path("downloads"),
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Download install.ps1 and manifest.json from the server."""
    try:
        with _client(base_url, token, config_file) as client:
            bootstrap = client.windows_bootstrap()
            manifest = client.windows_manifest()
            install_path = client.download_file(
                bootstrap.install_script_url,
                output_dir / "install.ps1",
            )
            manifest_path = client.download_file(
                bootstrap.manifest_url,
                output_dir / "manifest.json",
            )
            downloaded = [install_path, manifest_path]
            for filename in manifest.files:
                if filename in {"install.ps1", "manifest.json"}:
                    continue
                url = f"{bootstrap.downloads_base_url}/{filename}"
                downloaded.append(client.download_file(url, output_dir / filename))
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(f"[green]Downloaded {len(downloaded)} file(s) to[/green] {output_dir.resolve()}")
    for path in downloaded:
        console.print(f"- {path.name} ({path.stat().st_size} bytes)")


@windows_app.command("setup")
def windows_setup(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
    as_json: CommonJson = False,
) -> None:
    """Configure local rclone remote from server bootstrap (Windows only)."""
    from file_sorting_client.windows.mount import is_windows
    from file_sorting_client.windows.setup import setup_from_api

    if not is_windows():
        console.print("[red]This command is only supported on Windows.[/red]")
        raise typer.Exit(code=1)

    settings = _resolve_settings(base_url, token, config_file)
    if not settings.api_token:
        console.print("[red]API token is required.[/red]")
        raise typer.Exit(code=1)

    try:
        state = setup_from_api(settings)
    except ApiError as exc:
        _handle_api_error(exc)

    if as_json:
        print_json(state.__dict__)
    else:
        console.print(f"[green]Configured remote[/green] {state.remote_name} -> {state.dav_url}")
        console.print(f"Drive letter: {state.drive_letter}:")


@mount_app.command("start")
def mount_start(
    base_url: CommonBaseUrl = None,
    token: CommonToken = None,
    config_file: CommonConfig = None,
) -> None:
    """Start cached WebDAV mount (Windows only)."""
    from file_sorting_client.windows.mount import ClientState, is_windows, start_mount
    from file_sorting_client.windows.setup import setup_from_api

    if not is_windows():
        console.print("[red]Mount is only supported on Windows.[/red]")
        raise typer.Exit(code=1)

    state = ClientState.load()
    if state is None:
        settings = _resolve_settings(base_url, token, config_file)
        if not settings.api_token:
            console.print("[red]API token is required for first-time setup.[/red]")
            raise typer.Exit(code=1)
        try:
            state = setup_from_api(settings)
        except ApiError as exc:
            _handle_api_error(exc)

    try:
        start_mount(remote_name=state.remote_name, drive_letter=state.drive_letter)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Drive {state.drive_letter}:\\ mounted with on-demand cache.[/green]")


@mount_app.command("stop")
def mount_stop() -> None:
    """Stop cached WebDAV mount (Windows only)."""
    from file_sorting_client.windows.mount import ClientState, is_windows, stop_mount

    if not is_windows():
        console.print("[red]Mount is only supported on Windows.[/red]")
        raise typer.Exit(code=1)

    state = ClientState.load()
    if state is None:
        console.print("[red]Client is not configured. Run windows setup first.[/red]")
        raise typer.Exit(code=1)

    stop_mount(state.drive_letter)
    console.print(f"[green]Stopped mount on {state.drive_letter}:\\[/green]")


@mount_app.command("status")
def mount_status_cmd(as_json: CommonJson = False) -> None:
    """Show local mount status (Windows only)."""
    from file_sorting_client.windows.mount import ClientState, is_windows, mount_status

    if not is_windows():
        console.print("[red]Mount status is only supported on Windows.[/red]")
        raise typer.Exit(code=1)

    state = ClientState.load()
    letter = state.drive_letter if state else "S"
    result = mount_status(letter)
    if as_json:
        print_json(result)
    else:
        mounted = "mounted" if result["mounted"] else "not mounted"
        console.print(f"Drive {result['drive_letter']}:\\ is {mounted}")
        console.print(f"Cache: {result['cache_dir']}")
        console.print(f"Log: {result['mount_log']}")


@mount_app.command("cleanup-cache")
def mount_cleanup_cache() -> None:
    """Remove stale local cache files (Windows only)."""
    from file_sorting_client.windows.mount import cleanup_cache, is_windows

    if not is_windows():
        console.print("[red]Cache cleanup is only supported on Windows.[/red]")
        raise typer.Exit(code=1)

    removed = cleanup_cache()
    console.print(f"[green]Removed {removed} stale cache file(s).[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
