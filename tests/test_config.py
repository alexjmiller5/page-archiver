import pytest
from pydantic import ValidationError

from page_archiver.config import Settings


def test_defaults_use_standard_state_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    settings = Settings()
    assert settings.state_dir.is_absolute()
    assert settings.state_dir.name == "page-archiver"
    assert settings.state_dir.is_relative_to(tmp_path)
    assert settings.capture_timeout == 90
    assert settings.max_artifact_bytes == 50 * 1024 * 1024


def test_explicit_configuration_is_portable_and_secret_is_redacted(tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state",
        hub_url="https://hub.example/",
        hub_token="fixture-private-value",
    )
    assert settings.hub_url == "https://hub.example"
    assert "fixture-private-value" not in repr(settings)
    assert "fixture-private-value" not in settings.model_dump_json()
    assert settings.state_dir == tmp_path / "state"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@hub.example",
        "file:///tmp/hub",
        "https://hub.example/?token=x",
        "http://hub.example",
    ],
)
def test_hub_endpoint_rejects_credential_bearing_or_insecure_urls(url):
    with pytest.raises(ValidationError):
        Settings(hub_url=url)


def test_status_settings_do_not_execute_credential_command(tmp_path):
    marker = tmp_path / "called"
    settings = Settings(credential_command=["touch", str(marker)])
    assert settings.credential_command == ["touch", str(marker)]
    assert not marker.exists()


def test_config_file_settings_with_environment_override(tmp_path, monkeypatch):
    from page_archiver.config import Settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "page-archiver" / "config.json"
    config.parent.mkdir()
    config.write_text('{"capture_timeout": 30, "max_artifact_bytes": 2048}')
    monkeypatch.setenv("PAGE_ARCHIVER_CAPTURE_TIMEOUT", "45")
    settings = Settings()
    assert settings.capture_timeout == 45
    assert settings.max_artifact_bytes == 2048


@pytest.mark.parametrize(
    "settings",
    [
        {"subscription_id": "../unexpected"},
        {"capture_table": "rows; SELECT 1"},
        {"artifact_prefix": "captures"},
        {"artifact_prefix": "captures/../"},
        {"artifact_prefix": "captures/%2f/"},
        {"artifact_prefix": "captures//"},
    ],
)
def test_hub_selectors_and_key_prefixes_are_canonical(settings):
    with pytest.raises(ValueError):
        Settings(**settings)
