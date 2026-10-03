import stat

import pytest

from page_archiver.state import Store


def batch(delivery="delivery-1", event="event-1", seq="1", url="https://example.com/a"):
    return {
        "subscription_id": "subscription-1",
        "delivery_id": delivery,
        "through_seq": seq,
        "events": [
            {
                "id": event,
                "seq": seq,
                "operation": "insert",
                "recorded_at": "2026-01-01T00:00:00Z",
                "source": {
                    "table": "items",
                    "row_id": "row-1",
                    "before_revision": None,
                    "after_revision": {
                        "updated_at": "2026-01-01T00:00:00Z",
                        "hub_at": "2026-01-01T00:00:01Z",
                    },
                },
                "changes": [{"column": "url", "old_value": None, "new_value": url}],
            }
        ],
    }


def test_batch_and_ack_receipt_survive_restart_with_private_state(tmp_path):
    with Store(tmp_path / "state") as store:
        store.accept_batch(batch())
    with Store(tmp_path / "state") as store:
        assert store.pending_ack("subscription-1") == {
            "delivery_id": "delivery-1",
            "through_seq": "1",
        }
        assert store.status() == {
            "events": 1,
            "queued": 1,
            "pending_acks": 1,
            "active": 0,
            "succeeded": 0,
            "failed": 0,
            "runtime": {},
        }
        assert store.jobs()[0]["url"] == "https://example.com/a"
    assert stat.S_IMODE((tmp_path / "state/inbox.sqlite3").stat().st_mode) == 0o600


def test_duplicate_delivery_is_harmless_but_conflicting_payload_is_rejected(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        store.accept_batch(batch())
        with pytest.raises(ValueError, match="conflict"):
            store.accept_batch(batch(url="https://example.com/other"))
        assert store.status()["queued"] == 1
        assert store.jobs()[0]["url"] == "https://example.com/a"


def test_malformed_batch_never_partially_commits_or_creates_ack(tmp_path):
    body = batch()
    body["events"].append({"id": "broken"})
    with Store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.accept_batch(body)
        assert store.status() == {
            "events": 0,
            "queued": 0,
            "pending_acks": 0,
            "active": 0,
            "succeeded": 0,
            "failed": 0,
            "runtime": {},
        }


def test_ack_is_explicit_and_only_clears_matching_receipt(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        with pytest.raises(ValueError):
            store.acknowledge("subscription-1", "wrong")
        assert store.pending_ack("subscription-1") is not None
        store.acknowledge("subscription-1", "delivery-1")
        assert store.pending_ack("subscription-1") is None
        assert store.status()["queued"] == 1


def test_fast_url_replacement_preserves_both_values_and_delete_does_not_capture(tmp_path):
    a = batch()
    b = batch("delivery-2", "event-2", "2", "https://example.com/b")
    deletion = batch("delivery-3", "event-3", "3", None)
    deletion["events"][0]["operation"] = "delete"
    deletion["events"][0]["source"]["after_revision"] = None
    with Store(tmp_path) as store:
        for body in (a, b, deletion):
            store.accept_batch(body)
            store.acknowledge("subscription-1", body["delivery_id"])
        assert {job["url"] for job in store.jobs()} == {
            "https://example.com/a",
            "https://example.com/b",
        }
        assert store.status() == {
            "events": 3,
            "queued": 2,
            "pending_acks": 0,
            "active": 0,
            "succeeded": 0,
            "failed": 0,
            "runtime": {},
        }


def test_new_delivery_cannot_replace_unacknowledged_receipt(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        with pytest.raises(ValueError, match="pending"):
            store.accept_batch(batch("delivery-2", "event-2", "2"))
        assert store.status()["events"] == 1


def test_conflicting_existing_event_rolls_back_whole_new_batch(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        store.acknowledge("subscription-1", "delivery-1")
        body = batch("delivery-2", "event-2", "2")
        collision = batch(event="event-1", seq="3", url="https://example.com/changed")["events"][0]
        body["events"].append(collision)
        body["through_seq"] = "3"
        with pytest.raises(ValueError, match="conflict"):
            store.accept_batch(body)
        assert store.status() == {
            "events": 1,
            "queued": 1,
            "pending_acks": 0,
            "active": 0,
            "succeeded": 0,
            "failed": 0,
            "runtime": {},
        }


def test_capacity_overflow_rolls_back_all_events_jobs_and_ack(tmp_path):
    body = batch()
    body["events"].append(batch(event="event-2", seq="2")["events"][0])
    body["through_seq"] = "2"
    with Store(tmp_path) as store:
        with pytest.raises(ValueError, match="queue_capacity"):
            store.accept_batch(body, max_pending_jobs=1)
        assert store.status()["events"] == 0
        assert store.pending_ack("subscription-1") is None
        store.accept_batch(batch(), max_pending_jobs=1)
        store.accept_batch(batch(), max_pending_jobs=1)
        assert store.status()["queued"] == 1


def test_attempt_identity_and_outcome_survive_restart_without_recapturing(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        job = store.next_job()
        assert job["source"]["table"] == "items"
        attempt = store.begin_attempt(job["capture_id"])
        assert store.next_job() is None
    with Store(tmp_path) as store:
        assert store.pending_attempt()["id"] == attempt["id"]
        assert store.begin_attempt(job["capture_id"])["id"] == attempt["id"]
        store.save_outcome(attempt["id"], {"status": "blocked"})
    with Store(tmp_path) as store:
        assert store.pending_attempt()["outcome"] == {"status": "blocked"}
        store.finish_attempt(attempt["id"])
        assert store.pending_attempt() is None
        assert store.status()["failed"] == 1
        assert store.status()["queued"] == 0
        assert store.status()["active"] == 0


def test_new_attempt_only_after_previous_published_and_explicit_retry(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        job = store.next_job()
        first = store.begin_attempt(job["capture_id"])
        with pytest.raises(ValueError, match="outcome_required"):
            store.finish_attempt(first["id"])
        store.save_outcome(first["id"], {"status": "timeout"})
        store.finish_attempt(first["id"], retry=True)
        second = store.begin_attempt(job["capture_id"])
        assert second["id"] != first["id"]
        assert second["number"] == 2
        assert store.jobs()[0]["event_id"] == "event-1"
        store.save_outcome(second["id"], {"status": "succeeded"})
        store.finish_attempt(second["id"])
        assert store.status()["succeeded"] == 1
        assert store.next_job() is None


def test_backfill_is_deduplicated_by_observation_and_live_jobs_run_first(tmp_path):
    row = {
        "id": "old",
        "url": "https://example.com/old",
        "updated_at": "2026-01-01T00:00:00Z",
        "hub_at": None,
        "deleted_at": None,
    }
    with Store(tmp_path) as store:
        old = store.enqueue_backfill("subscription-1", "items", row, "url")
        assert store.enqueue_backfill("subscription-1", "items", row, "url") == old
        assert len(store.jobs()) == 1
        store.accept_batch(batch())
        assert store.next_job()["event_id"] == "event-1"
        changed = store.enqueue_backfill(
            "subscription-1", "items", {**row, "url": "https://example.com/new"}, "url"
        )
        assert old != changed
        assert len(store.jobs()) == 3
        assert (
            store.enqueue_backfill(
                "subscription-1", "items", {**row, "deleted_at": "deleted"}, "url"
            )
            is None
        )


def test_runner_lock_is_exclusive_but_status_and_backfill_connections_remain_usable(tmp_path):
    with Store(tmp_path) as first, Store(tmp_path) as second:
        with first.runner_lock():
            with pytest.raises(RuntimeError, match="runner_already_active"):
                with second.runner_lock():
                    pytest.fail("two runners acquired the same state directory")
            assert second.status()["queued"] == 0
        with second.runner_lock():
            assert second.status()["queued"] == 0


def test_only_one_active_attempt_is_possible_and_outcomes_are_immutable(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        store.acknowledge("subscription-1", "delivery-1")
        store.accept_batch(batch("delivery-2", "event-2", "2"))
        jobs = store.jobs()
        attempt = store.begin_attempt(jobs[0]["capture_id"])
        with pytest.raises(ValueError, match="attempt_already_active"):
            store.begin_attempt(jobs[1]["capture_id"])
        store.save_outcome(attempt["id"], {"status": "blocked"})
        store.save_outcome(attempt["id"], {"status": "blocked"})
        with pytest.raises(ValueError, match="outcome_conflict"):
            store.save_outcome(attempt["id"], {"status": "succeeded"})


def test_existing_foundation_database_upgrades_without_losing_pending_work(tmp_path):
    import json
    import sqlite3

    with sqlite3.connect(tmp_path / "inbox.sqlite3") as db:
        db.executescript("""
            CREATE TABLE events(subscription_id TEXT,event_id TEXT,payload TEXT,
                PRIMARY KEY(subscription_id,event_id));
            CREATE TABLE pending_acks(subscription_id TEXT PRIMARY KEY,delivery_id TEXT,through_seq TEXT,payload TEXT);
            CREATE TABLE jobs(capture_id TEXT PRIMARY KEY,subscription_id TEXT,event_id TEXT,
                source_column TEXT,url TEXT,state TEXT DEFAULT 'queued',
                UNIQUE(subscription_id,event_id,source_column));
        """)
        body = batch()
        db.execute(
            "INSERT INTO events VALUES (?,?,?)",
            ("subscription-1", "event-1", json.dumps(body["events"][0])),
        )
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            ("stable-job", "subscription-1", "event-1", "url", "https://example.com/a", "queued"),
        )
        db.execute(
            "INSERT INTO pending_acks VALUES (?,?,?,?)",
            ("subscription-1", "delivery-1", "1", json.dumps(body)),
        )
    with Store(tmp_path) as store:
        assert store.next_job()["capture_id"] == "stable-job"
        assert store.pending_ack("subscription-1")["delivery_id"] == "delivery-1"
        assert store.begin_attempt("stable-job")["number"] == 1
    with Store(tmp_path) as store:
        assert store.pending_attempt()["capture_id"] == "stable-job"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_runner_lock_excludes_another_process(tmp_path):
    import subprocess
    import sys

    script = """
import sys
from pathlib import Path
from page_archiver.state import Store
try:
    with Store(Path(sys.argv[1])) as store, store.runner_lock():
        print('acquired')
except RuntimeError:
    print('locked')
"""
    with Store(tmp_path) as store:
        with store.runner_lock():
            result = subprocess.run(
                [sys.executable, "-c", script, str(tmp_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == "locked"
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "acquired"
