import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from page_archiver.capture import Artifact, Outcome
from page_archiver.client import HubError
from page_archiver.config import Settings
from page_archiver.publication import publish_attempt


class FakeHub:
    def __init__(self):
        self.files = {}
        self.metadata = {}
        self.uploads = []
        self.fail_upload = None
        self.lose_insert = False

    async def rows(self, table, columns, *, where=None, after=None):
        return {
            "rows": [
                r
                for r in self.metadata.values()
                if all(r.get(k) == v for k, v in (where or {}).items())
            ],
            "next_cursor": None,
        }

    async def upload(self, key, path, mime, sha256):
        self.uploads.append(key)
        if self.fail_upload and key.endswith(self.fail_upload):
            self.fail_upload = None
            raise HubError("hub_unavailable")
        data = Path(path).read_bytes()
        self.files.setdefault(
            key,
            {
                "mime": mime,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "etag": "opaque",
            },
        )
        return None

    async def head(self, key):
        return self.files[key]

    async def insert(self, table, row):
        self.metadata.setdefault(row["id"], row)
        if self.lose_insert:
            self.lose_insert = False
            raise HubError("hub_unavailable")
        return {"inserted": [row["id"]], "existing": [], "rejected": []}


def staged(directory):
    directory.mkdir(parents=True)
    outcome = Outcome(status="succeeded", captured_at="2026-01-01T00:00:01.123456Z")
    for kind, data, mime in [
        ("html", b"<h1>Saved</h1>", "text/html"),
        ("png", b"fixture-png", "image/png"),
    ]:
        path = directory / ("page." + kind)
        path.write_bytes(data)
        setattr(
            outcome,
            kind,
            Artifact(
                path=str(path), mime=mime, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
            ),
        )
    (directory / "capture.json").write_text(outcome.model_dump_json())
    return outcome


def context(tmp_path):
    settings = Settings(state_dir=tmp_path, capture_table="captures", artifact_prefix="captures/")
    job = {
        "capture_id": "capture-1",
        "event_id": "event-1",
        "subscription_id": "subscription-1",
        "source_column": "url",
        "url": "https://example.test/a",
        "source": {
            "table": "articles",
            "row_id": "row-1",
            "after_revision": {"updated_at": "2026-01-01T00:00:00.000Z", "hub_at": None},
        },
    }
    attempt = {"id": "attempt-1", "started_at": "2026-01-01T00:00:00.000000+00:00"}
    return settings, job, attempt


def test_partial_upload_and_lost_metadata_response_reuse_identical_artifacts(tmp_path):
    settings, job, attempt = context(tmp_path)
    outcome = staged(tmp_path / "spool/attempt-1")
    hub = FakeHub()
    hub.fail_upload = "page.png"

    async def check():
        with pytest.raises(HubError):
            await publish_attempt(hub, settings, job, attempt, outcome)
        assert not hub.metadata and len(hub.files) == 1
        hub.lose_insert = True
        with pytest.raises(HubError):
            await publish_attempt(hub, settings, job, attempt, outcome)
        assert len(hub.metadata) == 1 and len(hub.files) == 2
        before = list(hub.uploads)
        row = await publish_attempt(hub, settings, job, attempt, outcome)
        assert len(hub.metadata) == 1 and hub.uploads == before
        assert row["status"] == "succeeded"
        assert row["failure_code"] is None
        assert row["captured_at"] == "2026-01-01T00:00:01.123Z"
        assert row["updated_at"].endswith(".123Z")
        assert row["html_key"] == "captures/capture-1/attempt-1/page.html"

    asyncio.run(check())


def test_changed_staged_bytes_or_existing_key_mismatch_never_publish_success(tmp_path):
    settings, job, attempt = context(tmp_path)
    outcome = staged(tmp_path / "spool/attempt-1")
    hub = FakeHub()
    Path(outcome.html.path).write_bytes(b"changed")
    with pytest.raises(HubError, match="staged_artifact_mismatch"):
        asyncio.run(publish_attempt(hub, settings, job, attempt, outcome))
    assert not hub.metadata and not hub.files
    Path(outcome.html.path).write_bytes(b"<h1>Saved</h1>")
    hub.files["captures/capture-1/attempt-1/page.html"] = {
        "mime": "text/html",
        "bytes": 999,
        "sha256": "0" * 64,
    }
    with pytest.raises(HubError, match="artifact_conflict"):
        asyncio.run(publish_attempt(hub, settings, job, attempt, outcome))
    assert not hub.metadata


def test_failed_capture_metadata_never_contains_artifact_fields(tmp_path):
    settings, job, attempt = context(tmp_path)
    hub = FakeHub()
    row = asyncio.run(
        publish_attempt(hub, settings, job, attempt, Outcome(status="login_required"))
    )
    assert row["status"] == "blocked" and row["failure_code"] == "login_required"
    assert row["captured_at"] is None and not hub.files
    assert all(v is None for k, v in row.items() if k.startswith(("html_", "png_")))
    assert json.loads(row["observed_source_revision"]) == job["source"]["after_revision"]


def test_existing_metadata_conflict_cannot_be_overwritten(tmp_path):
    settings, job, attempt = context(tmp_path)
    hub = FakeHub()
    hub.metadata["attempt-1"] = {
        "id": "attempt-1",
        "status": "succeeded",
        "source_url": "https://example.test/different",
    }
    with pytest.raises(HubError, match="metadata_conflict"):
        asyncio.run(publish_attempt(hub, settings, job, attempt, Outcome(status="blocked")))
    assert hub.metadata["attempt-1"]["source_url"].endswith("different")


@pytest.mark.parametrize("change", ["outside", "symlink", "bad_mime", "missing_png"])
def test_manifest_cannot_publish_escaped_or_incomplete_artifacts(tmp_path, change):
    settings, job, attempt = context(tmp_path)
    hub = FakeHub()
    outcome = staged(tmp_path / "spool/attempt-1")
    path = Path(outcome.html.path)
    if change == "outside":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_bytes(path.read_bytes())
        outcome.html.path = str(elsewhere)
    elif change == "symlink":
        elsewhere = tmp_path / "elsewhere"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
    elif change == "bad_mime":
        outcome.html.mime = "text/javascript"
    else:
        outcome.png = None
    with pytest.raises(HubError, match="staged_artifact_mismatch"):
        asyncio.run(publish_attempt(hub, settings, job, attempt, outcome))
    assert not hub.files and not hub.metadata


def test_metadata_matches_shared_client_fixture(tmp_path):
    from page_archiver.publication import metadata

    contract = json.loads((Path(__file__).parent / "fixtures/capture-metadata-v1.json").read_text())
    expected = contract["success"]
    config = Settings(state_dir=tmp_path, capture_table="captures", artifact_prefix="captures/")
    job = {k: expected[k] for k in ("capture_id", "event_id", "subscription_id", "source_column")}
    job["url"] = expected["source_url"]
    job["source"] = {
        "table": "articles",
        "row_id": "source-1",
        "after_revision": {
            "updated_at": "2026-01-01T00:00:00.000Z",
            "hub_at": "2026-01-01T00:00:00.001Z",
        },
    }
    attempt = {"id": expected["id"], "started_at": "2026-01-01T00:00:01.000000+00:00"}
    outcome = Outcome(
        status="succeeded",
        captured_at="2026-01-01T00:00:02.000000Z",
        html=Artifact(
            path="unused",
            mime="text/html",
            bytes=17,
            sha256="74c7835231c92b40bf6415ed21bb8e9cacfb624f16b8a15510a3984874594aa6",
        ),
        png=Artifact(path="unused", mime="image/png", bytes=100, sha256="a" * 64),
    )
    assert metadata(config, job, attempt, outcome) == expected
    failed = metadata(config, job, attempt, Outcome(status="login_required"))
    assert {k: failed[k] for k in contract["failure_fields"]} == contract["failure_fields"]
