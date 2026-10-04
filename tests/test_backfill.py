import asyncio

import pytest

from page_archiver.backfill import backfill, recapture
from page_archiver.client import HubError
from page_archiver.state import Store
from test_runner import settings


class SourceHub:
    def __init__(self):
        self.calls = []
        self.fail = True
        self.revision = "2026-01-01T00:00:00.000Z"

    async def subscription(self):
        return {"state": "active", "sources": [{"table": "articles", "columns": ["url"]}]}

    async def rows(self, table, columns, *, after=None, where=None):
        assert table == "articles" and set(columns) == {
            "id",
            "url",
            "updated_at",
            "hub_at",
            "deleted_at",
        }
        self.calls.append(after)
        if after == "row-1" and self.fail:
            raise HubError("hub_unavailable")
        row = {
            "id": "row-1" if after is None else "row-2",
            "url": "https://example.test/",
            "updated_at": self.revision,
            "hub_at": None,
            "deleted_at": None,
        }
        return {"rows": [row], "next_cursor": "row-1" if after is None and where is None else None}


def test_backfill_resumes_committed_page_and_repeated_observations_deduplicate(tmp_path):
    config = settings(tmp_path)
    hub = SourceHub()
    with Store(tmp_path) as store:
        with pytest.raises(HubError):
            asyncio.run(backfill(config, store, hub))
        assert len(store.jobs()) == 1
    hub.fail = False
    with Store(tmp_path) as store:
        asyncio.run(backfill(config, store, hub))
        assert hub.calls == [None, "row-1", "row-1"]
        assert len(store.jobs()) == 2
        asyncio.run(backfill(config, store, hub))
        assert len(store.jobs()) == 2
        hub.revision = "2026-01-02T00:00:00.000Z"
        asyncio.run(backfill(config, store, hub))
        assert len(store.jobs()) == 4
        capture_id = store.jobs()[0]["capture_id"]
        new_id = asyncio.run(recapture(config, store, hub, capture_id))
        assert new_id != capture_id and len(store.jobs()) == 5


def test_backfill_skips_empty_and_deleted_but_records_invalid_url_for_failure(tmp_path):
    class Pages(SourceHub):
        async def rows(self, *_, **__):
            return {
                "rows": [
                    {
                        "id": "empty",
                        "url": None,
                        "updated_at": None,
                        "hub_at": None,
                        "deleted_at": None,
                    },
                    {
                        "id": "deleted",
                        "url": "https://example.test",
                        "updated_at": None,
                        "hub_at": None,
                        "deleted_at": "2026-01-01T00:00:00.000Z",
                    },
                    {
                        "id": "invalid",
                        "url": "not a URL",
                        "updated_at": None,
                        "hub_at": None,
                        "deleted_at": None,
                    },
                ],
                "next_cursor": None,
            }

    with Store(tmp_path) as store:
        asyncio.run(backfill(settings(tmp_path), store, Pages()))
        assert len(store.jobs()) == 1 and store.jobs()[0]["url"] == "not a URL"


def test_backfill_cannot_skip_partial_page_when_capacity_is_reached(tmp_path):
    config = settings(tmp_path)
    config.max_pending_jobs = 1

    class Pages(SourceHub):
        async def rows(self, *_, **__):
            return {
                "rows": [
                    {
                        "id": str(i),
                        "url": "https://example.test",
                        "updated_at": None,
                        "hub_at": None,
                        "deleted_at": None,
                    }
                    for i in range(2)
                ],
                "next_cursor": None,
            }

    with Store(tmp_path) as store:
        with pytest.raises(ValueError, match="queue_capacity"):
            asyncio.run(backfill(config, store, Pages()))
        assert len(store.jobs()) == 1
        config.max_pending_jobs = 100
        asyncio.run(backfill(config, store, Pages()))
        assert len(store.jobs()) == 2
