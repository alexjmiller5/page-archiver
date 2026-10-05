import asyncio
import hashlib

import httpx
import pytest

from page_archiver.client import HubClient, HubError
from page_archiver.retrieval import retrieve
from test_client import settings


def test_download_checks_actual_bytes_and_does_not_follow_redirects(tmp_path):
    data = b"archived data"
    expected = {"mime": "text/html", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def check():
        for status, content, code in [
            (200, b"changed bytes", "artifact_conflict"),
            (302, b"", "redirect_refused"),
        ]:
            headers = {
                "Content-Type": "text/html",
                "Content-Length": str(len(content)),
                "X-Content-SHA256": expected["sha256"],
                "Location": "https://other.test/",
            }
            async with HubClient(
                settings(hub_token="fixture-secret"),
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(status, headers=headers, content=content)
                ),
            ) as hub:
                with pytest.raises(HubError, match=code):
                    await hub.download("captures/a/page.html", tmp_path / "page.html", expected)
                assert not (tmp_path / "page.html").exists()
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    headers={"Content-Type": "text/html", "X-Content-SHA256": expected["sha256"]},
                    content=data,
                )
            ),
        ) as hub:
            await hub.download("captures/a/page.html", tmp_path / "page.html", expected)
            assert (tmp_path / "page.html").read_bytes() == data
            with pytest.raises(HubError, match="destination_exists"):
                await hub.download("captures/a/page.html", tmp_path / "page.html", expected)

    asyncio.run(check())


def test_retrieve_requires_complete_success_and_removes_partial_downloads(tmp_path):
    class Hub:
        async def rows(self, table, columns, *, where):
            return {
                "rows": [
                    {
                        "id": "attempt-1",
                        "status": "succeeded",
                        **{
                            f"{kind}_{field}": value
                            for kind, mime in [("html", "text/html"), ("png", "image/png")]
                            for field, value in [
                                ("key", f"captures/a/page.{kind}"),
                                ("mime", mime),
                                ("bytes", 10),
                                ("sha256", "a" * 64),
                            ]
                        },
                    }
                ],
                "next_cursor": None,
            }

        async def download(self, key, path, expected):
            if key.endswith("png"):
                raise HubError("artifact_conflict", fatal=True)
            path.write_bytes(b"first")

    with pytest.raises(HubError, match="artifact_conflict"):
        asyncio.run(retrieve(Hub(), settings(), "attempt-1", tmp_path / "result"))
    assert not (tmp_path / "result").exists()


def test_chunked_download_verifies_stream_length_when_transport_omits_content_length(tmp_path):
    data = b"chunked archive"

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data[:5]
            yield data[5:]

    expected = {"mime": "text/html", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def check():
        async with HubClient(
            settings(hub_token="fixture-secret"),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    headers={
                        "Content-Type": "text/html",
                        "X-Content-SHA256": expected["sha256"],
                        "Transfer-Encoding": "chunked",
                    },
                    stream=Chunks(),
                )
            ),
        ) as hub:
            await hub.download("captures/a/page.html", tmp_path / "page.html", expected)
        assert (tmp_path / "page.html").read_bytes() == data

    asyncio.run(check())


def test_retrieve_partial_capture_preserves_warning_and_both_artifacts(tmp_path):
    import json

    class Hub:
        async def rows(self, table, columns, *, where):
            return {
                "rows": [
                    {
                        "id": "attempt-1",
                        "status": "partial",
                        "failure_code": "partial",
                        "failure_detail": "Incomplete archive: 2 resource requests could not be saved.",
                        **{f"{kind}_key": f"captures/a/page.{kind}" for kind in ("html", "png")},
                    }
                ]
            }

        async def download(self, key, path, expected):
            path.write_bytes(b"fixture")

    result = asyncio.run(retrieve(Hub(), settings(), "attempt-1", tmp_path / "result"))
    assert result["status"] == "partial"
    assert result["warning"].startswith("Incomplete archive:")
    assert (tmp_path / "result/page.html").exists() and (tmp_path / "result/page.png").exists()
    assert (
        json.loads((tmp_path / "result/metadata.json").read_text())["failure_detail"]
        == result["warning"]
    )
