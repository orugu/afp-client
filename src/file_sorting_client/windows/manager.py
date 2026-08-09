from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from file_sorting_client.api import FileSortingApiClient
from file_sorting_client.config import ClientSettings
from file_sorting_client.windows.mount import (
    ClientState,
    cleanup_cache,
    drive_is_mounted,
    mount_status,
    start_mount,
    stop_mount,
)
from file_sorting_client.windows.setup import setup_from_api


class ManagerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("File Sorting Manager")
        self.root.geometry("520x420")
        self.root.resizable(False, False)

        self.settings = ClientSettings.load()
        self.state = ClientState.load()
        self.status_var = tk.StringVar(value="Ready")
        self.drive_var = tk.StringVar(value=self._drive_letter())
        self.mount_var = tk.StringVar(value="Unknown")

        self._build_ui()
        self.refresh_status()

    def _drive_letter(self) -> str:
        if self.state:
            return self.state.drive_letter
        return "S"

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="File Sorting Client", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Server files are mounted with on-demand cache.\n"
            "Files download when opened and sync back after edits.",
            wraplength=480,
        ).pack(anchor="w", pady=(4, 12))

        info = ttk.LabelFrame(frame, text="Status", padding=12)
        info.pack(fill="x", pady=(0, 12))
        ttk.Label(info, text="Drive").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.drive_var).grid(row=0, column=1, sticky="w")
        ttk.Label(info, text="Mount").grid(row=1, column=0, sticky="w")
        ttk.Label(info, textvariable=self.mount_var).grid(row=1, column=1, sticky="w")
        ttk.Label(info, text="Message").grid(row=2, column=0, sticky="w")
        ttk.Label(info, textvariable=self.status_var, wraplength=360).grid(row=2, column=1, sticky="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 12))
        ttk.Button(buttons, text="Setup / Reconnect", command=self.run_setup).pack(side="left")
        ttk.Button(buttons, text="Mount", command=self.run_mount).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Unmount", command=self.run_unmount).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open Drive", command=self.open_drive).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Clean Cache", command=self.run_cleanup).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh_status).pack(side="left", padx=(8, 0))

        config = ttk.LabelFrame(frame, text="API Settings", padding=12)
        config.pack(fill="x")
        ttk.Label(config, text="API Base URL").grid(row=0, column=0, sticky="w", pady=4)
        self.base_url_entry = ttk.Entry(config, width=48)
        self.base_url_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.base_url_entry.insert(0, self.settings.api_base_url)

        ttk.Label(config, text="API Token").grid(row=1, column=0, sticky="w", pady=4)
        self.token_entry = ttk.Entry(config, width=48, show="*")
        self.token_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.token_entry.insert(0, self.settings.api_token)
        config.columnconfigure(1, weight=1)

        ttk.Button(frame, text="Save Settings", command=self.save_settings).pack(anchor="e", pady=(12, 0))

    def save_settings(self) -> None:
        self.settings = self.settings.with_overrides(
            api_base_url=self.base_url_entry.get().strip(),
            api_token=self.token_entry.get().strip(),
        )
        self.settings.save()
        self.status_var.set("Settings saved.")

    def refresh_status(self) -> None:
        self.state = ClientState.load()
        letter = self._drive_letter()
        self.drive_var.set(f"{letter}:\\")
        status = mount_status(letter)
        mounted = "Mounted" if status["mounted"] else "Not mounted"
        self.mount_var.set(mounted)

    def _require_state(self) -> ClientState | None:
        self.state = ClientState.load()
        if self.state:
            return self.state
        messagebox.showwarning("Not installed", "Run Setup / Reconnect first.")
        return None

    def _run_async(self, label: str, fn) -> None:
        self.status_var.set(label)

        def worker() -> None:
            try:
                fn()
                self.root.after(0, lambda: self.status_var.set(f"{label} completed."))
                self.root.after(0, self.refresh_status)
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda: messagebox.showerror("Error", message))
                self.root.after(0, lambda: self.status_var.set(message))

        threading.Thread(target=worker, daemon=True).start()

    def run_setup(self) -> None:
        def action() -> None:
            self.save_settings()
            if not self.settings.api_token:
                raise RuntimeError("API token is required.")
            with FileSortingApiClient(self.settings) as client:
                client.health()
            self.state = setup_from_api(self.settings)

        self._run_async("Setting up client", action)

    def run_mount(self) -> None:
        state = self._require_state()
        if not state:
            return

        def action() -> None:
            start_mount(
                remote_name=state.remote_name,
                drive_letter=state.drive_letter,
            )

        self._run_async("Starting mount", action)

    def run_unmount(self) -> None:
        state = self._require_state()
        if not state:
            return
        self._run_async("Stopping mount", lambda: stop_mount(state.drive_letter))

    def run_cleanup(self) -> None:
        def action() -> None:
            removed = cleanup_cache()
            self.root.after(0, lambda: self.status_var.set(f"Removed {removed} stale cache file(s)."))

        self._run_async("Cleaning cache", action)

    def open_drive(self) -> None:
        letter = self._drive_letter()
        if not drive_is_mounted(letter):
            messagebox.showinfo("Drive not mounted", "Mount the drive first.")
            return
        subprocess.Popen(["explorer.exe", f"{letter}:\\"])

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if os.name != "nt":
        print("FileSortingManager is intended for Windows.", file=sys.stderr)
        sys.exit(1)
    ManagerApp().run()


if __name__ == "__main__":
    main()
