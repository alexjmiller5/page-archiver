"""Read-only published history and current-source coverage, never capture intake."""

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime

from .client import HubError
from .state import encoded


COLUMNS = [
    "id",
    "capture_id",
    "event_id",
    "subscription_id",
    "source_table",
    "source_row_id",
    "source_column",
    "source_url",
    "observed_source_revision",
    "attempted_at",
    "captured_at",
    "status",
    "failure_code",
    "failure_detail",
    "deleted_at",
]
STATUSES = ("succeeded", "partial", "blocked", "unsupported", "failed")


def now():
    return datetime.now(UTC).isoformat()


def observation(row):
    return tuple(row[k] for k in ("source_table", "source_row_id", "source_column", "source_url"))


async def page(hub, table, columns, *, after=None, where=None):
    result = await hub.rows(table, columns, after=after, where=where)
    rows, cursor = result["rows"], result["next_cursor"]
    previous = after
    for row in rows:
        identity = row.get("id")
        if (
            not isinstance(identity, str)
            or not identity
            or (previous is not None and identity <= previous)
        ):
            raise HubError("invalid_rows", fatal=True)
        previous = identity
    if cursor is not None and (not rows or cursor != rows[-1]["id"]):
        raise HubError("invalid_rows", fatal=True)
    return rows, cursor


async def scan(hub, table, columns, max_pages):
    cursor = None
    for _ in range(max_pages):
        rows, cursor = await page(hub, table, columns, after=cursor)
        for row in rows:
            yield row
        if cursor is None:
            return
    raise HubError("scan_limit_exceeded", fatal=True)


def validate_capture(row):
    if (
        any(
            not isinstance(row.get(k), str) or not row[k]
            for k in (
                "id",
                "source_table",
                "source_row_id",
                "source_column",
                "source_url",
                "attempted_at",
            )
        )
        or row.get("status") not in STATUSES
        or (row["status"] in {"succeeded", "partial"} and not row.get("captured_at"))
    ):
        raise HubError("invalid_capture_metadata", fatal=True)


async def list_captures(hub, settings, args):
    where = {
        field: getattr(args, option)
        for field, option in (
            ("source_table", "source_table"),
            ("source_row_id", "source_row"),
            ("source_column", "source_column"),
            ("source_url", "url"),
            ("status", "status"),
        )
        if getattr(args, option) is not None
    }
    rows, cursor = await page(
        hub, settings.capture_table, COLUMNS, after=args.after, where=where or None
    )
    matches = []
    for index, row in enumerate(rows):
        if row["deleted_at"] is not None:
            continue
        validate_capture(row)
        if args.command == "search" and args.query not in row["source_url"]:
            continue
        matches.append(row)
        if len(matches) == args.limit:
            if index < len(rows) - 1 or cursor is not None:
                cursor = row["id"]
            break
    return {
        "captures": matches,
        "next_cursor": cursor,
        "order": "id_ascending",
        "artifacts_verified": False,
    }


def pending_snapshot(settings):
    """Read an existing bound queue without creating/migrating it or consuming events."""
    path = settings.state_dir / "inbox.sqlite3"
    if not path.is_file():
        return None, "missing"
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            stored = db.execute("SELECT configuration FROM consumer_identity WHERE id=1").fetchone()
            expected = encoded(
                [
                    settings.hub_url,
                    settings.subscription_id,
                    settings.capture_table,
                    settings.artifact_prefix,
                ]
            )
            if stored is None or stored[0] != expected:
                return None, "identity_unavailable"
            pending = set()
            for column, url, payload in db.execute(
                "SELECT j.source_column,j.url,e.payload FROM jobs j "
                "JOIN events e USING(subscription_id,event_id) "
                "WHERE j.subscription_id=? AND j.state IN ('queued','active')",
                (settings.subscription_id,),
            ):
                source = json.loads(payload)["source"]
                pending.add((source["table"], source["row_id"], column, url))
            return pending, None
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return None, "unavailable"


async def coverage(hub, settings, *, max_pages=1000):
    started = now()
    subscription = await hub.subscription()
    history = {}
    async for row in scan(hub, settings.capture_table, COLUMNS, max_pages):
        if row["deleted_at"] is not None:
            continue
        validate_capture(row)
        key = observation(row)
        retained, latest = history.get(key, (None, None))
        if latest is None or (row["attempted_at"], row["id"]) > (
            latest["attempted_at"],
            latest["id"],
        ):
            latest = row
        if row["status"] in {"succeeded", "partial"} and (
            retained is None
            or (row["status"] == "succeeded", row["captured_at"], row["id"])
            > (retained["status"] == "succeeded", retained["captured_at"], retained["id"])
        ):
            retained = row
        history[key] = retained, latest
    pending, queue_reason = pending_snapshot(settings)
    queue_at = now()
    observations = []
    seen = set()
    for selection in subscription["sources"]:
        table = selection["table"]
        columns = list(
            dict.fromkeys(["id", "updated_at", "hub_at", "deleted_at", *selection["columns"]])
        )
        async for row in scan(hub, table, columns, max_pages):
            if row["deleted_at"] is not None:
                continue
            for column in selection["columns"]:
                url = row[column]
                if url is not None and not isinstance(url, str):
                    raise HubError("invalid_rows", fatal=True)
                if not url:
                    continue
                key = table, row["id"], column, url
                if key in seen:
                    continue
                seen.add(key)
                retained, latest = history.get(key, (None, None))
                waiting = key in pending if pending is not None else None
                status = (
                    retained["status"]
                    if retained
                    else "pending"
                    if waiting
                    else "pending_unknown"
                    if waiting is None
                    else "failed"
                    if latest
                    else "uncaptured"
                )
                observations.append(
                    {
                        "source_table": table,
                        "source_row_id": row["id"],
                        "source_column": column,
                        "source_url": url,
                        "source_revision": {
                            "updated_at": row["updated_at"],
                            "hub_at": row["hub_at"],
                        },
                        "coverage": status,
                        "pending": waiting,
                        "retained_attempt_id": retained["id"] if retained else None,
                        "captured_at": retained["captured_at"] if retained else None,
                        "observed_source_revision": retained["observed_source_revision"]
                        if retained
                        else None,
                        "latest_attempt_id": latest["id"] if latest else None,
                        "latest_status": latest["status"] if latest else None,
                        "failure_code": latest["failure_code"] if latest else None,
                        "warning": retained["failure_detail"] if retained else None,
                    }
                )
    counts = Counter(row["coverage"] for row in observations)
    return {
        "observations": observations,
        "counts": {
            s: counts[s]
            for s in ("succeeded", "partial", "pending", "failed", "uncaptured", "pending_unknown")
        },
        "queue_visibility": "known" if pending is not None else "unknown",
        "queue_reason": queue_reason,
        "queue_observed_at": queue_at,
        "scan_started_at": started,
        "scan_finished_at": now(),
        "atomic_snapshot": False,
        "artifacts_verified": False,
        "basis": "current_source_exact_url",
    }
