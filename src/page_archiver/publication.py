"""Immutable artifact publication. A terminal row follows verified file creation."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from .capture import Outcome
from .client import HubError
from .config import Settings
from .state import encoded

ARTIFACTS = {"html": "text/html", "png": "image/png"}
FAILURES = {
    "browser_unavailable",
    "partial",
    "blocked",
    "login_required",
    "empty",
    "asset_unavailable",
    "too_large",
    "http_error",
    "blocked_destination",
    "unsupported_content",
    "invalid_url",
    "destination_exists",
    "invalid_artifact",
    "timeout",
    "browser_error",
    "storage_error",
}


def timestamp(value: str) -> str:
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError()
        return instant.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        raise HubError("invalid_capture_timestamp", fatal=True) from None


def metadata(settings: Settings, job: dict, attempt: dict, outcome: Outcome) -> dict:
    if outcome.status != "succeeded" and outcome.status not in FAILURES:
        raise HubError("invalid_capture_outcome", fatal=True)
    succeeded = outcome.status == "succeeded"
    partial = outcome.status == "partial" and outcome.captured_at is not None
    retained = succeeded or partial
    if partial and outcome.missing_resources < 1:
        raise HubError("invalid_capture_outcome", fatal=True)
    attempted_at = timestamp(attempt["started_at"])
    captured_at = timestamp(outcome.captured_at) if retained else None
    status = (
        "succeeded"
        if succeeded
        else "partial"
        if partial
        else "blocked"
        if outcome.status in {"blocked", "login_required"}
        else "unsupported"
        if outcome.status
        in {"unsupported_content", "invalid_url", "blocked_destination", "too_large"}
        else "failed"
    )
    row = {
        "id": attempt["id"],
        "capture_id": job["capture_id"],
        "event_id": job["event_id"],
        "subscription_id": job["subscription_id"],
        "source_table": job["source"]["table"],
        "source_row_id": job["source"]["row_id"],
        "source_column": job["source_column"],
        "source_url": job["url"],
        "observed_source_revision": encoded(job["source"]["after_revision"]),
        "attempted_at": attempted_at,
        "captured_at": captured_at,
        "status": status,
        "failure_code": None if succeeded else outcome.status,
        "failure_detail": f"Incomplete archive: {outcome.missing_resources} resource requests could not be saved."
        if partial
        else None,
        "created_at": attempted_at,
        "updated_at": captured_at or attempted_at,
        "deleted_at": None,
    }
    for kind, mime in ARTIFACTS.items():
        artifact = getattr(outcome, kind) if retained else None
        if retained and (artifact is None or artifact.mime != mime):
            raise HubError("staged_artifact_mismatch", fatal=True)
        row.update(
            {
                f"{kind}_key": f"{settings.artifact_prefix}{job['capture_id']}/{attempt['id']}/page.{kind}"
                if retained
                else None,
                f"{kind}_mime": artifact.mime if artifact else None,
                f"{kind}_bytes": artifact.bytes if artifact else None,
                f"{kind}_sha256": artifact.sha256 if artifact else None,
            }
        )
    return row


def validate_staged(settings: Settings, attempt: dict, outcome: Outcome) -> None:
    directory = settings.state_dir / "spool" / attempt["id"]
    try:
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError()
        for kind, mime in ARTIFACTS.items():
            artifact = getattr(outcome, kind)
            path = directory / f"page.{kind}"
            if (
                artifact is None
                or artifact.mime != mime
                or Path(artifact.path) != path
                or path.is_symlink()
                or not path.is_file()
                or not 0 < artifact.bytes <= settings.max_artifact_bytes
                or path.stat().st_size != artifact.bytes
                or re.fullmatch(r"[a-f0-9]{64}", artifact.sha256) is None
            ):
                raise ValueError()
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != artifact.sha256:
                    raise ValueError()
    except (ValueError, OSError):
        raise HubError("staged_artifact_mismatch", fatal=True) from None


async def verify_remote(hub, row: dict) -> None:
    for kind in ARTIFACTS:
        actual = await hub.head(row[f"{kind}_key"])
        if any(
            actual.get(field) != row[f"{kind}_{field}"] for field in ("mime", "bytes", "sha256")
        ):
            raise HubError("artifact_conflict", fatal=True)


async def existing(hub, settings: Settings, row: dict) -> bool:
    result = await hub.rows(settings.capture_table, list(row), where={"id": row["id"]})
    if not result["rows"]:
        return False
    if len(result["rows"]) != 1 or any(result["rows"][0].get(k) != v for k, v in row.items()):
        raise HubError("metadata_conflict", fatal=True)
    return True


async def publish_attempt(
    hub, settings: Settings, job: dict, attempt: dict, outcome: Outcome
) -> dict:
    row = metadata(settings, job, attempt, outcome)
    if await existing(hub, settings, row):
        if row["status"] in {"succeeded", "partial"}:
            await verify_remote(hub, row)
        return row
    if row["status"] in {"succeeded", "partial"}:
        validate_staged(settings, attempt, outcome)
        for kind in ARTIFACTS:
            artifact = getattr(outcome, kind)
            await hub.upload(
                row[f"{kind}_key"], Path(artifact.path), artifact.mime, artifact.sha256
            )
        await verify_remote(hub, row)
    result = await hub.insert(settings.capture_table, row)
    # Read back even after an apparently successful insert. An insert-only conflict
    # or an ambiguous response must never silently adopt a different row.
    if not await existing(hub, settings, row):
        if any(isinstance(r, dict) and r.get("retryable") for r in result["rejected"]):
            raise HubError("metadata_unavailable")
        raise HubError("metadata_rejected", fatal=True)
    return row
