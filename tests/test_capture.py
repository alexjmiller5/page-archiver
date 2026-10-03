import asyncio
import hashlib
import struct
from pathlib import Path

import pytest

from page_archiver.capture import CaptureFailure, Rendered, capture
from page_archiver.config import Settings


PNG = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 1280, 900) + b"fixture"
HTML = "<!DOCTYPE html><html><head><title>Fixture</title></head><body>Saved page</body></html>"


def test_capture_publishes_both_artifacts_and_checksums(tmp_path, monkeypatch):
    async def render(*args):
        return Rendered(HTML, PNG, "https://example.com/final", "Fixture", 0)

    monkeypatch.setattr("page_archiver.capture._render", render)
    destination = tmp_path / "capture"
    result = asyncio.run(capture("https://example.com/", destination, Settings()))
    assert result.status == "succeeded"
    assert result.html.mime == "text/html"
    assert result.png.mime == "image/png"
    for artifact, content in [(result.html, HTML.encode()), (result.png, PNG)]:
        assert Path(artifact.path).read_bytes() == content
        assert artifact.bytes == len(content)
        assert artifact.sha256 == hashlib.sha256(content).hexdigest()
        assert Path(artifact.path).stat().st_mode & 0o777 == 0o600
    assert result.captured_at.endswith("Z")
    assert (destination / "capture.json").exists()


@pytest.mark.parametrize("failure", ["timeout", "blocked", "too_large", "browser_unavailable"])
def test_failed_capture_has_no_success_keys_or_staged_files(tmp_path, monkeypatch, failure):
    async def render(*args):
        raise CaptureFailure(failure)

    monkeypatch.setattr("page_archiver.capture._render", render)
    result = asyncio.run(capture("https://example.com/", tmp_path / "capture", Settings()))
    assert result.status == failure
    assert result.html is None and result.png is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "html,png,status",
    [("", PNG, "empty"), (HTML * 100, PNG, "too_large"), (HTML, b"not a PNG", "invalid_artifact")],
)
def test_invalid_artifacts_do_not_publish_partial_results(tmp_path, monkeypatch, html, png, status):
    async def render(*args):
        return Rendered(html, png, "https://example.com/", "Fixture", 0)

    monkeypatch.setattr("page_archiver.capture._render", render)
    result = asyncio.run(
        capture("https://example.com/", tmp_path / "capture", Settings(max_artifact_bytes=1024))
    )
    assert result.status == status
    assert not list(tmp_path.iterdir())


def test_existing_capture_is_not_overwritten(tmp_path, monkeypatch):
    destination = tmp_path / "capture"
    destination.mkdir()
    (destination / "keep").write_text("immutable")
    result = asyncio.run(capture("https://example.com/", destination, Settings()))
    assert result.status == "destination_exists"
    assert (destination / "keep").read_text() == "immutable"


def test_invalid_url_is_rejected_without_launching_browser(tmp_path, monkeypatch):
    async def unexpected(*args):
        pytest.fail("browser should not start")

    monkeypatch.setattr("page_archiver.capture._render", unexpected)
    result = asyncio.run(capture("http://127.0.0.1/", tmp_path / "capture", Settings()))
    assert result.status == "invalid_url"
