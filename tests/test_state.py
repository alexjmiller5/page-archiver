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
        assert store.status() == {"events": 1, "queued": 1, "pending_acks": 1}
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
        assert store.status() == {"events": 0, "queued": 0, "pending_acks": 0}


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
        assert store.status() == {"events": 3, "queued": 2, "pending_acks": 0}


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
        assert store.status() == {"events": 1, "queued": 1, "pending_acks": 0}
