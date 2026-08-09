from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from file_sorting_client.models import (
    FileDecision,
    WindowsBootstrapResponse,
    WindowsConnectivityResponse,
    WindowsDownloadsValidationResponse,
    WorkerStateResponse,
)

console = Console()


def print_json(data: Any) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def print_health(ok: bool, worker_running: bool) -> None:
    status = Text("OK" if ok else "FAIL", style="green" if ok else "red")
    worker = Text("RUNNING" if worker_running else "STOPPED", style="green" if worker_running else "yellow")
    console.print(Panel(f"API: {status}\nWorker: {worker}", title="Health"))


def print_worker_status(status: WorkerStateResponse) -> None:
    running = "RUNNING" if status.running else "STOPPED"
    style = "green" if status.running else "yellow"
    console.print(
        Panel(
            "\n".join(
                [
                    f"[bold]Worker[/bold]: [{style}]{running}[/{style}]",
                    f"[bold]Scanned[/bold]: {status.scanned_files}",
                    f"[bold]Moved[/bold]: {status.moved_files}",
                    f"[bold]Failed[/bold]: {status.failed_files}",
                    f"[bold]Last scan[/bold]: {status.last_scan_at or '-'}",
                ]
            ),
            title="Worker Status",
        )
    )

    if status.recent_decisions:
        print_decisions_table(status.recent_decisions[:20], title="Recent Decisions")


def print_decisions_table(items: list[FileDecision], *, title: str = "Search Results") -> None:
    table = Table(title=title, show_lines=True)
    table.add_column("Time", style="dim")
    table.add_column("File")
    table.add_column("Category")
    table.add_column("Status")
    table.add_column("Reason")

    for item in items:
        table.add_row(
            item.timestamp,
            item.filename,
            item.category or "-",
            item.status,
            item.reason or "-",
        )

    console.print(table)


def print_bootstrap(info: WindowsBootstrapResponse) -> None:
    table = Table(title="Windows Bootstrap", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    for key, value in info.model_dump().items():
        table.add_row(key, str(value))
    console.print(table)


def print_connectivity(result: WindowsConnectivityResponse) -> None:
    overall = "OK" if result.ok else "FAIL"
    style = "green" if result.ok else "red"
    lines = [
        f"[bold]Overall[/bold]: [{style}]{overall}[/{style}]",
        f"[bold]Manifest[/bold]: {'OK' if result.manifest_ok else 'FAIL'}",
        f"[bold]WebDAV[/bold]: {'OK' if result.dav_ok else 'FAIL'}",
    ]
    if result.errors:
        lines.append("")
        lines.append("[bold red]Errors[/bold red]:")
        lines.extend(f"- {error}" for error in result.errors)
    console.print(Panel("\n".join(lines), title="Connectivity"))


def print_download_validation(result: WindowsDownloadsValidationResponse) -> None:
    overall = "OK" if result.ok else "FAIL"
    style = "green" if result.ok else "red"
    console.print(
        Panel(
            "\n".join(
                [
                    f"[bold]Overall[/bold]: [{style}]{overall}[/{style}]",
                    f"[bold]Manifest[/bold]: {'OK' if result.manifest_ok else 'FAIL'}",
                    f"[bold]Version[/bold]: {result.version or '-'}",
                ]
            ),
            title="Download Validation",
        )
    )

    if result.files:
        table = Table(title="Files")
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("HTTP")
        table.add_column("Error")
        for item in result.files:
            status = "OK" if item.ok else "FAIL"
            table.add_row(item.name, status, str(item.status_code or "-"), item.error or "-")
        console.print(table)

    if result.errors:
        console.print("[bold red]Errors[/bold red]")
        for error in result.errors:
            console.print(f"- {error}")
