"""Portable, side-effect-free runtime configuration."""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, SettingsConfigDict


def default_state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/page-archiver"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "page-archiver"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAGE_ARCHIVER_", hide_input_in_errors=True)

    state_dir: Path = Field(default_factory=default_state_dir)
    hub_url: str | None = None
    hub_token: SecretStr | None = Field(default=None, exclude=True)
    credential_command: list[str] | None = None
    browser_executable: Path | None = None
    capture_timeout: int = Field(default=90, ge=1, le=600)
    max_artifact_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=250 * 1024 * 1024)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        path = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "page-archiver/config.json"
        )
        return (
            init_settings,
            env_settings,
            JsonConfigSettingsSource(settings_cls, json_file=path),
            file_secret_settings,
        )

    @field_validator("state_dir", "browser_executable")
    @classmethod
    def expand_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().absolute() if value is not None else None

    @field_validator("hub_url")
    @classmethod
    def valid_hub(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("hub_url must be an HTTPS origin without credentials")
        return value.rstrip("/")
