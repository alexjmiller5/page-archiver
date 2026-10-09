import json

from page_archiver.main import main


def test_status_uses_configured_state_and_never_reads_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PAGE_ARCHIVER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PAGE_ARCHIVER_CREDENTIAL_COMMAND", '["false"]')
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "events": 0,
        "queued": 0,
        "pending_acks": 0,
        "active": 0,
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "runtime": {},
    }


def test_capture_cli_emits_manifest_and_failure_exit_code(monkeypatch, tmp_path, capsys):
    from page_archiver.capture import Outcome

    async def capture(url, destination, settings):
        assert url == "https://example.com/"
        assert destination == tmp_path / "result"
        return Outcome(status="blocked")

    monkeypatch.setattr("page_archiver.main.capture", capture)
    assert main(["capture", "https://example.com/", "--output", str(tmp_path / "result")]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_invalid_config_is_sanitized_and_watch_requires_consumer_settings(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PAGE_ARCHIVER_HUB_URL", "https://fixture-secret@example.test")
    assert main(["watch"]) == 78
    out = capsys.readouterr().out
    assert json.loads(out)["error"] == "invalid_configuration" and "fixture-secret" not in out
    monkeypatch.delenv("PAGE_ARCHIVER_HUB_URL")
    assert main(["watch"]) == 78
    assert json.loads(capsys.readouterr().out)["error"] == "hub_configuration_required"


def test_watch_retries_startup_cap_without_waiting_weeks(monkeypatch, tmp_path):
    import asyncio
    from page_archiver.client import HubError
    from page_archiver.runner import retry_hub
    from page_archiver.state import Store

    delays = []
    attempts = []

    async def action():
        attempts.append(1)
        if len(attempts) == 1:
            raise HubError("hub_capped", retry_after=2_000_000)
        return "ready"

    async def sleep(delay):
        delays.append(delay)

    with Store(tmp_path) as store:
        assert asyncio.run(retry_hub(action, store, "intake", sleep=sleep)) == "ready"
    assert delays == [3600]


def test_watch_survives_network_outage_during_initial_session_check(monkeypatch, tmp_path, capsys):
    from page_archiver.client import HubError
    from page_archiver.runner import retry_hub
    from test_client import settings

    calls = []
    delays = []

    class Hub:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def session(self):
            calls.append("session")
            if calls.count("session") == 1:
                raise HubError("hub_unavailable")

        async def subscription(self):
            calls.append("subscription")

    class Runner:
        def __init__(self, *_):
            pass

        async def run(self):
            calls.append("watch")

    async def sleep(delay):
        delays.append(delay)

    async def retry(action, store, component):
        return await retry_hub(action, store, component, sleep=sleep)

    monkeypatch.setattr(
        "page_archiver.main.Settings",
        lambda: settings(state_dir=tmp_path, hub_token="fixture-secret"),
    )
    monkeypatch.setattr("page_archiver.main.HubClient", Hub)
    monkeypatch.setattr("page_archiver.main.Runner", Runner)
    monkeypatch.setattr("page_archiver.main.retry_hub", retry)
    assert main(["watch"]) == 0
    assert calls == ["session", "session", "subscription", "watch"] and delays == [1]


def test_run_once_drains_existing_work_under_intake_backpressure(monkeypatch, tmp_path, capsys):
    import copy
    from page_archiver.capture import Outcome
    from page_archiver.runner import Runner
    from page_archiver.state import Store
    from test_client import settings
    from test_publication import FakeHub
    from test_state import batch

    config = settings(state_dir=tmp_path, hub_token="fixture-secret", max_pending_jobs=100)
    body = batch()
    body["subscription_id"] = config.subscription_id
    example = body["events"][0]
    body["events"] = []
    for i in range(1, 101):
        event = copy.deepcopy(example)
        event["id"] = f"event-{i}"
        event["seq"] = str(i)
        body["events"].append(event)
    body["through_seq"] = "100"
    with Store(tmp_path) as store:
        store.accept_batch(body)
        store.acknowledge(config.subscription_id, body["delivery_id"])
    offered = copy.deepcopy(body)
    offered["events"] = [copy.deepcopy(example)]
    offered["events"][0].update(id="event-101", seq="101")
    offered.update(delivery_id="delivery-101", through_seq="101")

    class Hub(FakeHub):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def session(self):
            return {}

        async def subscription(self):
            return {}

        async def poll(self, wait=0):
            return offered

        async def ack(self, delivery_id):
            return "101"

    async def blocked(*_):
        return Outcome(status="blocked")

    hub = Hub()
    monkeypatch.setattr("page_archiver.main.Settings", lambda: config)
    monkeypatch.setattr("page_archiver.main.HubClient", lambda _: hub)
    monkeypatch.setattr(
        "page_archiver.main.Runner", lambda s, db, h: Runner(s, db, h, capture_fn=blocked)
    )
    for _ in range(2):
        assert main(["run-once"]) == 0
    with Store(tmp_path) as store:
        assert store.status()["failed"] == 2 and store.status()["queued"] == 99
        assert store.status()["events"] == 101 and store.status()["pending_acks"] == 0
    assert len(hub.metadata) == 2


def test_move_hub_rebinds_after_the_subscription_answers_at_the_new_url(
    monkeypatch, tmp_path, capsys
):
    from page_archiver.state import Store
    from test_client import settings

    configured = settings(state_dir=tmp_path, hub_token="fixture-secret")
    with Store(tmp_path) as store:
        store.bind_consumer(
            "https://old.test",
            configured.subscription_id,
            configured.capture_table,
            configured.artifact_prefix,
        )
    calls = []

    class Hub:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def session(self):
            calls.append("session")

        async def subscription(self):
            calls.append("subscription")

    monkeypatch.setattr("page_archiver.main.Settings", lambda: configured)
    monkeypatch.setattr("page_archiver.main.HubClient", Hub)
    assert main(["move-hub"]) == 0
    assert calls == ["session", "subscription"]
    assert json.loads(capsys.readouterr().out) == {"hub_url": configured.hub_url}
    with Store(tmp_path) as store:
        store.bind_consumer(
            configured.hub_url,
            configured.subscription_id,
            configured.capture_table,
            configured.artifact_prefix,
        )
