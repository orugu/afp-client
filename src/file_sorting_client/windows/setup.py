from __future__ import annotations

from file_sorting_client.api import FileSortingApiClient
from file_sorting_client.config import ClientSettings
from file_sorting_client.windows.mount import ClientState, configure_rclone_remote, is_windows, normalize_dav_url


def setup_from_api(settings: ClientSettings) -> ClientState:
    if not is_windows():
        raise RuntimeError("Windows setup is only supported on Windows.")

    with FileSortingApiClient(settings) as client:
        bootstrap = client.windows_bootstrap()
        credentials = client.windows_credentials()

    configure_rclone_remote(
        remote_name=bootstrap.remote_name,
        dav_url=bootstrap.dav_url,
        username=credentials.username,
        password=credentials.password,
    )

    state = ClientState(
        app_base_url=bootstrap.app_base_url,
        api_base_url=bootstrap.api_base_url,
        api_token=settings.api_token,
        dav_url=normalize_dav_url(bootstrap.dav_url),
        remote_name=bootstrap.remote_name,
        drive_letter=bootstrap.drive_letter,
        webdav_username=credentials.username,
        webdav_password=credentials.password,
        installed_at=__import__("datetime").datetime.now().isoformat(),
        version="0.1.1",
    )
    state.save()
    return state
