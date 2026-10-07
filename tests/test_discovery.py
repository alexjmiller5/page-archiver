import json
from pathlib import Path

import pytest

from page_archiver.client import HubError
from page_archiver.main import main
from page_archiver.state import Store
from test_client import settings


FIXTURE = json.loads((Path(__file__).parent / "fixtures/capture-metadata-v1.json").read_text())


def attempt(identity, source="source-1", status="succeeded", **changes):
    row = {**FIXTURE["success"], "id": identity, "source_row_id": source}
    if status == "partial":
        row.update(FIXTURE["partial_fields"])
    elif status != "succeeded":
        row.update(FIXTURE["failure_fields"], status=status)
    return {**row, **changes}


def source(identity, url="https://example.test/article", **changes):
    return dict(id=identity, url=url, updated_at=None, hub_at=None, deleted_at=None, **changes)


class ReadHub:
    def __init__(self, config, captures=(), sources=(), page_size=2):
        self.config, self.captures, self.sources = config, captures, sources
        self.page_size = page_size

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def session(self):
        return {}

    async def subscription(self):
        return {"state": "active", "sources": [{"table": "articles", "columns": ["url"]}]}

    async def rows(self, table, columns, *, after=None, where=None):
        rows = self.captures if table == self.config.capture_table else self.sources
        rows = sorted((r for r in rows if after is None or r["id"] > after), key=lambda r: r["id"])
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        page = rows[: self.page_size]
        return {
            "rows": [{c: r.get(c) for c in columns} for r in page],
            "next_cursor": page[-1]["id"] if len(rows) > len(page) else None,
        }


def run(monkeypatch, capsys, config, hub, args):
    monkeypatch.setattr("page_archiver.main.Settings", lambda: config)
    monkeypatch.setattr("page_archiver.main.HubClient", lambda _: hub)
    code = main(args)
    return code, json.loads(capsys.readouterr().out)


def test_cli_lists_history_with_bounded_continuation_without_creating_local_state(
    monkeypatch, capsys, tmp_path
):
    config = settings(state_dir=tmp_path / "absent")
    hub = ReadHub(config, [attempt("a"), attempt("b", status="partial"), attempt("c")])
    code, page = run(monkeypatch, capsys, config, hub, ["list", "--limit", "1"])
    assert code == 0
    assert [r["id"] for r in page["captures"]] == ["a"]
    assert page["next_cursor"] == "a"
    _, page = run(monkeypatch, capsys, config, hub, ["list", "--after", "a"])
    assert [r["id"] for r in page["captures"]] == ["b", "c"]
    assert page["next_cursor"] is None
    assert not config.state_dir.exists()


def test_search_is_literal_and_keeps_a_cursor_even_when_page_has_no_matches(
    monkeypatch, capsys, tmp_path
):
    config = settings(state_dir=tmp_path)
    hub = ReadHub(
        config, [attempt("a"), attempt("b"), attempt("c", source_url="https://example.test/%_")]
    )
    code, page = run(monkeypatch, capsys, config, hub, ["search", "%_"])
    assert code == 0 and page["captures"] == [] and page["next_cursor"] == "b"
    _, page = run(monkeypatch, capsys, config, hub, ["search", "%_", "--after", "b"])
    assert [r["id"] for r in page["captures"]] == ["c"]


def test_list_source_status_and_exact_url_filters_preserve_deleted_source_history(
    monkeypatch, capsys, tmp_path
):
    config = settings(state_dir=tmp_path)
    hub = ReadHub(config, [attempt("a"), attempt("b", status="partial")])
    code, page = run(
        monkeypatch,
        capsys,
        config,
        hub,
        [
            "list",
            "--source-table",
            "articles",
            "--source-row",
            "source-1",
            "--source-column",
            "url",
            "--status",
            "partial",
            "--url",
            "https://example.test/article",
        ],
    )
    assert code == 0 and [r["id"] for r in page["captures"]] == ["b"]


def test_coverage_preserves_best_archive_and_separates_current_urls_and_pending(
    monkeypatch, capsys, tmp_path
):
    config = settings(state_dir=tmp_path)
    captures = [
        attempt("a"),
        attempt("b", status="partial"),
        attempt("c", status="failed", attempted_at="2026-02-01T00:00:00.000Z"),
        attempt("d", "source-2", status="partial"),
        attempt("e", "source-3"),
        attempt("f", "deleted"),
        attempt("g", "source-5", status="unsupported"),
    ]
    rows = [
        source("source-1"),
        source("source-2"),
        source("source-3", "https://example.test/new"),
        source("source-4"),
        source("source-5"),
        source("empty", ""),
        source("nil", None),
        {**source("deleted"), "deleted_at": "2026-01-01"},
    ]
    with Store(tmp_path) as store:
        store.bind_consumer(
            config.hub_url, config.subscription_id, config.capture_table, config.artifact_prefix
        )
        store.enqueue_backfill(config.subscription_id, "articles", rows[0], "url")
        store.enqueue_backfill(config.subscription_id, "articles", rows[2], "url")
        before = store.db.total_changes
        code, report = run(
            monkeypatch, capsys, config, ReadHub(config, captures, rows), ["coverage"]
        )
        assert store.db.total_changes == before
        assert store.status()["queued"] == 2 and store.status()["pending_acks"] == 0
    assert code == 0 and report["queue_visibility"] == "known"
    assert report["counts"] == {
        "succeeded": 1,
        "partial": 1,
        "pending": 1,
        "failed": 1,
        "uncaptured": 1,
        "pending_unknown": 0,
    }
    by_id = {r["source_row_id"]: r for r in report["observations"]}
    assert by_id["source-1"]["retained_attempt_id"] == "a"
    assert by_id["source-1"]["latest_attempt_id"] == "c"
    assert by_id["source-1"]["pending"] is True
    assert by_id["source-3"]["retained_attempt_id"] is None
    assert report["atomic_snapshot"] is False
    assert report["scan_started_at"] <= report["scan_finished_at"]


@pytest.mark.parametrize("local", ["absent", "unbound", "mismatch", "corrupt"])
def test_coverage_unknown_queue_never_claims_idle(monkeypatch, capsys, tmp_path, local):
    config = settings(state_dir=tmp_path / "state")
    if local in {"unbound", "mismatch"}:
        with Store(config.state_dir) as store:
            if local == "mismatch":
                store.bind_consumer(
                    "https://other.test",
                    config.subscription_id,
                    config.capture_table,
                    config.artifact_prefix,
                )
    elif local == "corrupt":
        config.state_dir.mkdir()
        (config.state_dir / "inbox.sqlite3").write_bytes(b"not sqlite")
    code, report = run(
        monkeypatch,
        capsys,
        config,
        ReadHub(config, [attempt("a", status="failed")], [source("source-1")]),
        ["coverage"],
    )
    assert code == 0 and report["queue_visibility"] == "unknown"
    assert report["observations"][0]["pending"] is None
    assert report["observations"][0]["coverage"] == "pending_unknown"
    assert report["observations"][0]["latest_attempt_id"] == "a"
    if local == "absent":
        assert not config.state_dir.exists()


@pytest.mark.parametrize(
    "error",
    [
        HubError("credential_rejected", fatal=True),
        HubError("hub_capped", 3600),
        HubError("hub_unavailable"),
    ],
)
def test_read_errors_never_become_empty_coverage(monkeypatch, capsys, tmp_path, error):
    config = settings(state_dir=tmp_path)

    class Denied(ReadHub):
        async def rows(self, *args, **kwargs):
            raise error

    code, body = run(monkeypatch, capsys, config, Denied(config), ["coverage"])
    assert code != 0 and body["error"] == error.code and "counts" not in body
    if error.retry_after:
        assert body["retry_after"] == 3600


def test_coverage_refuses_truncated_or_repeating_scan(monkeypatch, capsys, tmp_path):
    config = settings(state_dir=tmp_path)
    hub = ReadHub(config, [attempt("a"), attempt("b"), attempt("c")])
    code, body = run(monkeypatch, capsys, config, hub, ["coverage", "--max-pages", "1"])
    assert code != 0 and body["error"] == "scan_limit_exceeded"

    class Repeating(ReadHub):
        async def rows(self, *args, **kwargs):
            return {"rows": [attempt("a")], "next_cursor": "a"}

    code, body = run(monkeypatch, capsys, config, Repeating(config), ["coverage"])
    assert code != 0 and body["error"] == "invalid_rows"
