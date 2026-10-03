import asyncio

import pytest

from page_archiver.capture import Outcome
from page_archiver.client import HubError
from page_archiver.config import Settings
from page_archiver.runner import Runner
from page_archiver.state import Store
from test_publication import FakeHub, staged
from test_state import batch


class InboxHub(FakeHub):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.polls = 0
        self.acks = []
        self.fail_ack = False

    async def poll(self, wait=30):
        self.polls += 1
        return batch()

    async def ack(self, delivery_id):
        # Simulated server observes the commit before seeing the ACK request.
        assert self.store.pending_ack("subscription-1")["delivery_id"] == delivery_id
        assert len(self.store.jobs()) == 1
        self.acks.append(delivery_id)
        if self.fail_ack:
            self.fail_ack = False
            raise HubError("hub_unavailable")
        return "1"


def settings(tmp_path):
    # Store tests use opaque fixture IDs; the configured network ID is a real UUID.
    config = Settings(
        state_dir=tmp_path,
        capture_table="captures",
        artifact_prefix="captures/",
        subscription_id="11111111-1111-4111-8111-111111111111",
    )
    config.subscription_id = "subscription-1"
    return config


def test_lost_ack_response_retries_receipt_without_polling_or_duplicate_jobs(tmp_path):
    with Store(tmp_path) as store:
        hub = InboxHub(store)
        hub.fail_ack = True
        config = settings(tmp_path)
        # The real network client already verifies identity. Keep this fixture
        # consistent with it while exercising the runner's separate durable seam.
        config.subscription_id = "subscription-1"
        runner = Runner(config, store, hub)
        with pytest.raises(HubError):
            asyncio.run(runner.intake_once())
        assert store.pending_ack("subscription-1") is not None
        asyncio.run(runner.intake_once())
        assert store.pending_ack("subscription-1") is None
        assert hub.acks == ["delivery-1", "delivery-1"] and hub.polls == 1
        assert len(store.jobs()) == 1


def test_failed_local_commit_never_acknowledges(tmp_path, mocker):
    with Store(tmp_path) as store:
        hub = InboxHub(store)
        mocker.patch.object(store, "accept_batch", side_effect=ValueError("queue_capacity"))
        config = settings(tmp_path)
        config.subscription_id = "subscription-1"
        runner = Runner(config, store, hub)
        with pytest.raises(ValueError, match="queue_capacity"):
            asyncio.run(runner.intake_once())
        assert hub.acks == [] and store.jobs() == []


def test_restart_recovers_staged_manifest_without_launching_a_browser(tmp_path):
    with Store(tmp_path) as store:
        store.accept_batch(batch())
        attempt = store.begin_attempt(store.next_job()["capture_id"])
    staged(tmp_path / "spool" / attempt["id"])

    async def forbidden(*_):
        pytest.fail("recovery recaptured existing staged bytes")

    with Store(tmp_path) as store:
        hub = InboxHub(store)
        runner = Runner(settings(tmp_path), store, hub, capture_fn=forbidden)
        assert asyncio.run(runner.process_one())
        assert store.status()["succeeded"] == 1
        assert store.pending_attempt() is None
        assert len(hub.metadata) == 1 and len(hub.files) == 2
        assert not (tmp_path / "spool" / attempt["id"]).exists()


def test_transient_capture_retries_are_bounded_and_keep_distinct_attempts(tmp_path):
    async def timeout(*_):
        return Outcome(status="timeout")

    with Store(tmp_path) as store:
        store.accept_batch(batch())
        hub = InboxHub(store)
        runner = Runner(settings(tmp_path), store, hub, capture_fn=timeout)
        for _ in range(3):
            assert asyncio.run(runner.process_one())
        assert not asyncio.run(runner.process_one())
        assert store.status()["failed"] == 1
        assert len(hub.metadata) == 3
        assert all(r["status"] == "failed" and r["html_key"] is None for r in hub.metadata.values())


def test_wrong_ack_boundary_preserves_local_receipt(tmp_path):
    class WrongAck(InboxHub):
        async def ack(self, delivery_id):
            return "999"

    with Store(tmp_path) as store:
        config = settings(tmp_path)
        config.subscription_id = "subscription-1"
        with pytest.raises(HubError, match="invalid_acknowledgment"):
            asyncio.run(Runner(config, store, WrongAck(store)).intake_once())
        assert store.pending_ack("subscription-1") is not None


def test_existing_incomplete_spool_is_not_recaptured(tmp_path):
    async def forbidden(*_):
        pytest.fail("must not replace existing staged bytes")

    with Store(tmp_path) as store:
        store.accept_batch(batch())
        attempt = store.begin_attempt(store.next_job()["capture_id"])
        directory = tmp_path / "spool" / attempt["id"]
        directory.mkdir(parents=True)
        (directory / "page.html").write_text("unfinished")
        with pytest.raises(HubError, match="invalid_staged_manifest"):
            asyncio.run(
                Runner(
                    settings(tmp_path), store, InboxHub(store), capture_fn=forbidden
                ).process_one()
            )
        assert (directory / "page.html").read_text() == "unfinished"
        assert store.pending_attempt()["id"] == attempt["id"]


def test_long_cap_wait_is_bounded_and_fatal_auth_cancels_worker(tmp_path):
    async def check():
        delays = []
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class CappedThenDenied(InboxHub):
            async def poll(self, wait=30):
                await started.wait()
                self.polls += 1
                if self.polls == 1:
                    raise HubError("hub_capped", retry_after=2_000_000)
                raise HubError("credential_rejected", fatal=True)

        async def sleep(delay):
            delays.append(delay)
            await asyncio.sleep(0)

        async def slow(*_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with Store(tmp_path) as store:
            store.accept_batch(batch())
            store.acknowledge("subscription-1", "delivery-1")
            runner = Runner(
                settings(tmp_path), store, CappedThenDenied(store), capture_fn=slow, sleep=sleep
            )
            with pytest.raises(HubError, match="credential_rejected"):
                await asyncio.wait_for(runner.run(), 2)
            assert delays == [3600]
            assert cancelled.is_set()
            assert store.pending_attempt() is not None
            assert store.status()["runtime"]["intake"] == "credential_rejected"

    asyncio.run(check())


def test_intake_continues_during_single_capture_and_cancellation_retains_work(tmp_path):
    async def check():
        started = asyncio.Event()
        accepted = asyncio.Event()
        calls = []

        class NextInbox(InboxHub):
            async def poll(self, wait=30):
                await started.wait()
                body = batch()
                body["subscription_id"] = config.subscription_id
                body["delivery_id"] = "delivery-2"
                body["through_seq"] = "2"
                body["events"][0]["id"] = "event-2"
                body["events"][0]["seq"] = "2"
                return body

            async def ack(self, delivery_id):
                assert len(self.store.jobs()) == 2
                assert self.store.pending_ack(config.subscription_id) is not None
                accepted.set()
                await asyncio.Future()

        async def slow(*_):
            calls.append(1)
            started.set()
            await asyncio.Future()

        with Store(tmp_path) as store:
            store.accept_batch(batch())
            store.acknowledge("subscription-1", "delivery-1")
            config = settings(tmp_path)
            task = asyncio.create_task(
                Runner(config, store, NextInbox(store), capture_fn=slow).run()
            )
            await asyncio.wait_for(accepted.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == [1] and store.status()["queued"] == 1
            assert store.status()["active"] == 1 and store.status()["pending_acks"] == 1

    asyncio.run(check())


def test_capture_configuration_has_no_hub_credential(tmp_path):
    from pydantic import SecretStr

    async def inspect(url, destination, config):
        assert config.hub_token is None and config.credential_command is None
        return Outcome(status="blocked")

    with Store(tmp_path) as store:
        config = settings(tmp_path)
        config.hub_token = SecretStr("fixture-consumer-secret")
        config.credential_command = ["fixture-secret-reader"]
        store.accept_batch(batch())
        asyncio.run(Runner(config, store, InboxHub(store), capture_fn=inspect).process_one())
        assert store.status()["failed"] == 1


def test_queued_work_from_another_subscription_is_not_published(tmp_path):
    async def forbidden(*_):
        pytest.fail("wrong subscription reached renderer")

    with Store(tmp_path) as store:
        store.accept_batch(batch())
        config = settings(tmp_path)
        config.subscription_id = "22222222-2222-4222-8222-222222222222"
        with pytest.raises(HubError, match="wrong_subscription"):
            asyncio.run(Runner(config, store, InboxHub(store), capture_fn=forbidden).process_one())
        assert store.status()["queued"] == 1
