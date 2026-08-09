from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "file-sorting-client"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "settings.json"


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FILE_SORTING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_base_url: str = Field(
        default="http://localhost:18080/file_managing/api",
        description="Base URL for the Auto File Processor API",
    )
    api_token: str = Field(default="", description="Bearer token for API authentication")
    config_file: Path = Field(default=DEFAULT_CONFIG_FILE, exclude=True)

    def save(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "api_base_url": self.api_base_url.rstrip("/"),
            "api_token": self.api_token,
        }
        self.config_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, config_file: Path | None = None) -> "ClientSettings":
        path = config_file or DEFAULT_CONFIG_FILE
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(config_file=path, **data)
        return cls(config_file=path)

    def with_overrides(
        self,
        *,
        api_base_url: str | None = None,
        api_token: str | None = None,
    ) -> "ClientSettings":
        updates: dict[str, Any] = {}
        if api_base_url is not None:
            updates["api_base_url"] = api_base_url.rstrip("/")
        if api_token is not None:
            updates["api_token"] = api_token
        if not updates:
            return self
        return self.model_copy(update=updates)
