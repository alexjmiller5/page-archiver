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
