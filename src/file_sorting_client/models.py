from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class Event(BaseModel):
    timestamp: str
    level: str
    message: str


class FileDecision(BaseModel):
    timestamp: str
    filename: str
    source_path: str
    destination_path: Optional[str] = None
    category: Optional[str] = None
    reason: Optional[str] = None
    status: str


class WorkerStateResponse(BaseModel):
    running: bool
    scanned_files: int
    moved_files: int
    failed_files: int
    last_scan_at: Optional[str] = None
    events: List[Event] = Field(default_factory=list)
    recent_decisions: List[FileDecision] = Field(default_factory=list)


class HealthResponse(BaseModel):
    ok: bool
    worker_running: bool


class SearchResponse(BaseModel):
    items: List[FileDecision] = Field(default_factory=list)


class WindowsCredentialUpdateRequest(BaseModel):
    username: str
    password: str


class WindowsCredentialResponse(BaseModel):
    service_name: str
    username: str
    password: str
    updated_at: Optional[str] = None


class WindowsCredentialResetResponse(BaseModel):
    service_name: str
    username: str
    password_present: bool
    updated_at: Optional[str] = None


class WindowsBootstrapResponse(BaseModel):
    app_base_url: str
    api_base_url: str
    downloads_base_url: str
    install_script_url: str
    manifest_url: str
    installer_api_url: str = ""
    installer_filename: str = "FileSortingSetup.bat"
    dav_url: str
    webdav_username: str
    remote_name: str
    drive_letter: str


class WindowsConnectivityResponse(BaseModel):
    ok: bool
    manifest_ok: bool
    dav_ok: bool
    errors: List[str] = Field(default_factory=list)


class WindowsManifestResponse(BaseModel):
    manifest_url: str
    downloads_base_url: str
    version: str
    files: List[str] = Field(default_factory=list)


class WindowsDownloadFileCheck(BaseModel):
    name: str
    url: str
    ok: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


class WindowsDownloadsValidationResponse(BaseModel):
    ok: bool
    manifest_ok: bool
    manifest_url: str
    downloads_base_url: str
    version: Optional[str] = None
    files: List[WindowsDownloadFileCheck] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    watch_dir: str
    output_dir: str
    reference_dir: str
    poll_interval_seconds: int
    gemini_model: str
    gemini_enabled: bool
    database_url_masked: str
    webdav_username: str


class ActionResponse(BaseModel):
    ok: bool
    message: str


class DownloadedFile(BaseModel):
    name: str
    path: Path
    size_bytes: int


class BrowseEntry(BaseModel):
    name: str
    path: str
    type: str  # "dir" | "file"
    size: Optional[int] = None
    modified: Optional[str] = None
    thumbnail_kind: Optional[str] = None
    # Content hash (files only) -- lets sync_manager.diff_folder match by
    # content instead of by literal path, since the server reorganizes an
    # uploaded file into a category/project folder that generally does not
    # match its original relative path.
    sha256: Optional[str] = None


class BrowseResponse(BaseModel):
    path: str
    entries: List[BrowseEntry] = Field(default_factory=list)


class UploadResponse(BaseModel):
    ok: bool
    saved: List[str] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list)
