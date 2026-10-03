import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from page_archiver.client import HubClient, HubError, retry_delay
from page_archiver.config import Settings

SUB = "11111111-1111-4111-8111-111111111111"
CAPS = {
    "row_api": "v1",
    "schema": "none",
    "replica_sync": False,
    "subscriptions": "durable-pull-v1",
    "files": "opaque-key-v1",
}
SCOPES = [
    "tables:read:articles",
    "tables:read:captures",
    "tables:write:captures",
    "files:read:captures/",
    "files:write:captures/",
    f"subscriptions:consume:{SUB}",
]


def settings(**kwargs):
    return Settings(
        hub_url="https://hub.test",
        subscription_id=SUB,
        capture_table="captures",
        artifact_prefix="captures/",
        **kwargs,
    )


def run(fn):
    return asyncio.run(fn())


def test_credential_command_runs_once_and_redirects_never_receive_auth(tmp_path):
    marker = tmp_path / "calls"
    command = [
        sys.executable,
        "-c",
        'from pathlib import Path; p=Path(__import__("sys").argv[1]); '
        'p.write_text(p.read_text()+"x" if p.exists() else "x"); print("fixture-secret")',
        str(marker),
    ]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://unrelated.test/secret"})

    async def check():
        async with HubClient(
            settings(credential_command=command), transport=httpx.MockTransport(respond)
        ) as hub:
            for _ in range(2):
                with pytest.raises(HubError, match="redirect_refused"):
                    await hub.session()

    run(check)
    assert marker.read_text() == "x"
    assert len(requests) == 2
    assert all(r.url.host == "hub.test" for r in requests)
    assert all(r.headers["Authorization"] == "Bearer fixture-secret" for r in requests)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "2000000"}, 3600),
        ({"Retry-After": "0"}, 1),
        ({"Retry-After": "-4"}, 60),
        ({"Retry-After": "nonsense"}, 60),
        ({}, 60),
    ],
)
def test_cap_retry_clamps_to_one_hour(headers, expected):
    assert retry_delay(headers) == expected


def test_retry_after_http_date_uses_bounded_seconds():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert retry_delay({"Retry-After": format_datetime(now + timedelta(seconds=91))}, now) == 91


@pytest.mark.parametrize(
    ("status", "code", "fatal"),
    [
        (403, "credential_rejected", True),
        (401, "credential_rejected", True),
        (429, "hub_capped", False),
        (503, "hub_unavailable", False),
    ],
)
def test_errors_never_expose_upstream_bodies(status, code, fatal):
    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    status,
                    headers={"Retry-After": "2000000"},
                    json={"error": "secret-cookie-fixture"},
                )
            ),
        ) as hub:
            with pytest.raises(HubError) as caught:
                await hub.session()
        assert caught.value.code == code
        assert caught.value.fatal == fatal
        assert "secret-cookie" not in str(caught.value)
        if status == 429:
            assert caught.value.retry_after == 3600

    run(check)


def test_session_and_subscription_require_supported_protocol_and_narrow_grants():
    async def check():
        for changed in [
            {"capabilities": {**CAPS, "row_api": "v999"}},
            {"scopes": ["full"]},
            {"scopes": SCOPES[:-1]},
        ]:
            async with HubClient(
                settings(hub_token="fixture-secret"),
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "name": "opaque-consumer",
                            "scopes": SCOPES,
                            "capabilities": CAPS,
                            **changed,
                        },
                    )
                ),
            ) as hub:
                with pytest.raises(HubError):
                    await hub.session()
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=(
                        {"name": "opaque-consumer", "scopes": SCOPES, "capabilities": CAPS}
                        if request.url.path.endswith("/session")
                        else {
                            "id": SUB,
                            "protocol": "durable-pull-v1",
                            "state": "active",
                            "sources": [{"table": "denied", "columns": ["url"]}],
                        }
                    ),
                )
            ),
        ) as hub:
            await hub.session()
            with pytest.raises(HubError, match="source_not_authorized"):
                await hub.subscription()

    run(check)


def test_response_and_subscription_identity_bounds_prevent_acceptance():
    async def check():
        for body in [
            b"x" * (1_048_576 + 1),
            json.dumps(
                {"subscription_id": "wrong", "delivery_id": None, "through_seq": "0", "events": []}
            ).encode(),
        ]:
            async with HubClient(
                settings(hub_token="fixture-secret"),
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
            ) as hub:
                with pytest.raises(HubError):
                    await hub.poll()

    run(check)


def test_upload_uses_conditional_checksum_and_head_verifies_metadata(tmp_path):
    path = tmp_path / "artifact"
    path.write_bytes(b"fixture")
    checksum = hashlib.sha256(b"fixture").hexdigest()
    requests = []

    async def respond(request):
        requests.append(request)
        if request.method == "PUT":
            assert await request.aread() == b"fixture"
            return httpx.Response(412, json={"error": "file_exists"})
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/html",
                "Content-Length": "7",
                "X-Content-SHA256": checksum,
                "ETag": "opaque",
            },
        )

    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"), transport=httpx.MockTransport(respond)
        ) as hub:
            assert await hub.upload("captures/a b/page.html", path, "text/html", checksum) is None
            assert await hub.head("captures/a b/page.html") == {
                "mime": "text/html",
                "bytes": 7,
                "sha256": checksum,
                "etag": "opaque",
            }
            for key in ["other/a", "captures/../a", "captures/%2fsecret", "captures//a"]:
                with pytest.raises(HubError, match="invalid_file_key"):
                    await hub.head(key)

    run(check)
    assert requests[0].headers["If-None-Match"] == "*"
    assert requests[0].headers["X-Content-SHA256"] == checksum
    assert requests[0].url.raw_path == b"/v1/files/captures/a%20b/page.html"
    assert len(requests) == 2


def test_head_does_not_treat_etag_as_a_checksum():
    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"Content-Length": "7", "Content-Type": "text/html", "ETag": "0" * 64},
                )
            ),
        ) as hub:
            with pytest.raises(HubError, match="unverified_artifact"):
                await hub.head("captures/a")

    run(check)


def test_transport_failures_and_very_large_retry_headers_are_sanitized():
    assert retry_delay({"Retry-After": "9" * 5000}) == 3600

    def fail(_):
        raise httpx.ConnectError("private upstream diagnostic")

    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"), transport=httpx.MockTransport(fail)
        ) as hub:
            with pytest.raises(HubError, match="^hub_unavailable$"):
                await hub.session()

    run(check)


def test_ack_rows_and_insert_keep_explicit_boundaries():
    seen = []

    def respond(request):
        body = json.loads(request.content) if request.content else None
        seen.append((request.url.path, body))
        if request.url.path.endswith("/ack"):
            return httpx.Response(200, json={"acked_seq": "9007199254740993"})
        if request.url.path.endswith("/pull"):
            return httpx.Response(200, json={"rows": [], "next_cursor": None})
        return httpx.Response(200, json={"inserted": ["attempt-1"], "existing": [], "rejected": []})

    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"), transport=httpx.MockTransport(respond)
        ) as hub:
            assert await hub.ack("delivery-1") == "9007199254740993"
            assert (await hub.rows("captures", ["id"], where={"id": "attempt-1"}))["rows"] == []
            assert (await hub.insert("captures", {"id": "attempt-1"}))["inserted"] == ["attempt-1"]

    run(check)
    assert seen[0][1] == {"delivery_id": "delivery-1"}
    assert seen[1][1] == {
        "table": "captures",
        "columns": ["id"],
        "limit": 100,
        "where": {"id": "attempt-1"},
    }
    assert seen[2][1] == {"table": "captures", "columns": ["id"], "rows": [{"id": "attempt-1"}]}


@pytest.mark.parametrize(
    "change",
    ["missing_url", "missing_updated_at", "missing_hub_at", "bad_id", "bad_revision", "bad_url"],
)
def test_rows_reject_missing_requested_fields_and_invalid_source_types(change):
    row = {
        "id": "row-1",
        "url": "https://example.test",
        "updated_at": "2026-01-01T00:00:00.000Z",
        "hub_at": None,
        "deleted_at": None,
    }
    if change.startswith("missing_"):
        del row[change.removeprefix("missing_")]
    elif change == "bad_id":
        row["id"] = None
    elif change == "bad_revision":
        row["updated_at"] = {"not": "a timestamp"}
    else:
        row["url"] = ["https://example.test"]

    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"rows": [row], "next_cursor": None})
            ),
        ) as hub:
            with pytest.raises(HubError, match="invalid_rows"):
                await hub.rows("articles", ["id", "url", "updated_at", "hub_at", "deleted_at"])

    run(check)
