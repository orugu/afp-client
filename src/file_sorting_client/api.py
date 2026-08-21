from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from file_sorting_client.config import ClientSettings
from file_sorting_client.models import (
    ActionResponse,
    BrowseResponse,
    ConfigResponse,
    HashCheckResponse,
    HealthResponse,
    SearchResponse,
    UploadResponse,
    WindowsBootstrapResponse,
    WindowsConnectivityResponse,
    WindowsCredentialResetResponse,
    WindowsCredentialResponse,
    WindowsCredentialUpdateRequest,
    WindowsDownloadsValidationResponse,
    WindowsManifestResponse,
    WorkerStateResponse,
)


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class FileSortingApiClient:
    def __init__(self, settings: ClientSettings, *, timeout: float = 30.0) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.api_base_url.rstrip("/"),
            timeout=timeout,
            headers=self._auth_headers(),
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.settings.api_token:
            return {}
        return {"Authorization": f"Bearer {self.settings.api_token}"}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FileSortingApiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        headers = kwargs.pop("headers", {})
        if auth:
            headers = {**self._auth_headers(), **headers}
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise ApiError(response.status_code, detail)
        if not response.content:
            return {}
        return response.json()

    def health(self) -> HealthResponse:
        return HealthResponse.model_validate(self._request("GET", "/health", auth=False))

    def status(self) -> WorkerStateResponse:
        return WorkerStateResponse.model_validate(self._request("GET", "/status"))

    def config(self) -> ConfigResponse:
        return ConfigResponse.model_validate(self._request("GET", "/config"))

    def start_worker(self) -> ActionResponse:
        return ActionResponse.model_validate(self._request("POST", "/control/start"))

    def stop_worker(self) -> ActionResponse:
        return ActionResponse.model_validate(self._request("POST", "/control/stop"))

    def scan_once(self) -> ActionResponse:
        return ActionResponse.model_validate(self._request("POST", "/control/scan-once"))

    def search(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> SearchResponse:
        params: dict[str, str | int] = {"limit": limit}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        if status:
            params["status"] = status
        return SearchResponse.model_validate(self._request("GET", "/search", params=params))

    def windows_bootstrap(self) -> WindowsBootstrapResponse:
        return WindowsBootstrapResponse.model_validate(self._request("GET", "/windows/bootstrap"))

    def windows_connectivity(self) -> WindowsConnectivityResponse:
        return WindowsConnectivityResponse.model_validate(
            self._request("GET", "/windows/connectivity")
        )

    def windows_credentials(self) -> WindowsCredentialResponse:
        return WindowsCredentialResponse.model_validate(self._request("GET", "/windows/credentials"))

    def windows_set_credentials(self, username: str, password: str) -> WindowsCredentialResponse:
        payload = WindowsCredentialUpdateRequest(username=username, password=password)
        return WindowsCredentialResponse.model_validate(
            self._request("POST", "/windows/credentials", json=payload.model_dump())
        )

    def windows_reset_credentials(self) -> WindowsCredentialResetResponse:
        return WindowsCredentialResetResponse.model_validate(
            self._request("POST", "/windows/credentials/reset-default")
        )

    def windows_manifest(self) -> WindowsManifestResponse:
        return WindowsManifestResponse.model_validate(
            self._request("GET", "/windows/downloads/manifest")
        )

    def windows_validate_downloads(self) -> WindowsDownloadsValidationResponse:
        return WindowsDownloadsValidationResponse.model_validate(
            self._request("GET", "/windows/downloads/validate")
        )

    def download_file(self, url: str, destination: Path) -> Path:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code >= 400:
                raise ApiError(response.status_code, response.text.strip() or response.reason_phrase)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
        return destination

    def upload_files(self, paths: list[Path]) -> UploadResponse:
        """POSTs one or more local files to /files/upload for the server's
        worker to pick up from watch_dir. httpx.Client is safe to reuse
        across threads (see upload_watcher.py, which calls this
        concurrently) as long as each call opens its own file handles, which
        this does.
        """
        files = []
        opened = []
        try:
            for path in paths:
                handle = path.open("rb")
                opened.append(handle)
                files.append(("files", (path.name, handle, "application/octet-stream")))
            return UploadResponse.model_validate(self._request("POST", "/files/upload", files=files))
        finally:
            for handle in opened:
                handle.close()

    def check_hashes(self, hashes: list[str]) -> set[str]:
        """Asks the server which of these content hashes it already has
        (anywhere -- organized tree or trash), in one round trip. Use this
        instead of matching against browse() results: browse() deliberately
        skips _trash, so content that got correctly deduped into trash
        looks "missing" there and gets endlessly re-uploaded otherwise. See
        sync_manager.diff_folder and upload_watcher.UploadWatcher.
        """
        if not hashes:
            return set()
        response = HashCheckResponse.model_validate(
            self._request("POST", "/files/check-hashes", json={"hashes": list(hashes)})
        )
        return set(response.existing)

    def browse(self, path: str = "") -> BrowseResponse:
        params = {"path": path} if path else {}
        return BrowseResponse.model_validate(self._request("GET", "/files/browse", params=params))

    def browse_recursive(self, path: str = "") -> BrowseResponse:
        """Every file under `path` in one request -- see the server-side
        docstring on /files/browse-recursive. Older servers 404 this
        (route didn't exist yet); download_manager.list_files_recursive
        falls back to the slower one-call-per-directory walk in that case.
        """
        params = {"path": path} if path else {}
        return BrowseResponse.model_validate(self._request("GET", "/files/browse-recursive", params=params))

    def download_organized_file(self, remote_path: str, destination: Path) -> Path:
        """Downloads a file already sorted into the organized output tree
        (as opposed to `download_file`, which fetches arbitrary unauthenticated
        static assets like the Windows installer). Goes through the same
        authenticated client/session as everything else here.
        """
        response = self._client.get(
            "/files/download", params={"path": remote_path}, headers=self._auth_headers()
        )
        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise ApiError(response.status_code, detail)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination
