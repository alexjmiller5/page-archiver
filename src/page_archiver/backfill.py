"""Explicit current-row observations with a restartable keyset cursor."""

import uuid

from .client import HubError


async def backfill(settings, store, hub) -> dict:
    store.bind_consumer(
        settings.hub_url, settings.subscription_id, settings.capture_table, settings.artifact_prefix
    )
    subscription = await hub.subscription()
    if subscription["state"] != "active":
        raise HubError("subscription_not_active", fatal=True)
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS backfill_cursors (subscription_id TEXT, source_table TEXT, cursor TEXT, complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(subscription_id,source_table))"
    )
    store.db.commit()
    sub = settings.subscription_id
    sources = subscription["sources"]
    with store.transaction():
        # A completed invocation starts a new scan; an interrupted one resumes.
        done = {
            r["source_table"]
            for r in store.db.execute(
                "SELECT source_table FROM backfill_cursors WHERE subscription_id=? AND complete=1",
                (sub,),
            )
        }
        if done == {s["table"] for s in sources}:
            store.db.execute("DELETE FROM backfill_cursors WHERE subscription_id=?", (sub,))
    accepted = 0
    for source in sources:
        table = source["table"]
        checkpoint = store.db.execute(
            "SELECT cursor,complete FROM backfill_cursors WHERE subscription_id=? AND source_table=?",
            (sub, table),
        ).fetchone()
        if checkpoint and checkpoint["complete"]:
            continue
        cursor = checkpoint["cursor"] if checkpoint else None
        columns = list(
            dict.fromkeys(["id", "updated_at", "hub_at", "deleted_at", *source["columns"]])
        )
        while True:
            page = await hub.rows(table, columns, after=cursor)
            next_cursor = page["next_cursor"]
            if next_cursor is not None and (
                not page["rows"]
                or next_cursor != page["rows"][-1].get("id")
                or (cursor is not None and next_cursor <= cursor)
            ):
                raise HubError("invalid_rows", fatal=True)
            for row in page["rows"]:
                if (
                    not isinstance(row.get("id"), str)
                    or not row["id"]
                    or any(
                        column not in row
                        or (row[column] is not None and not isinstance(row[column], str))
                        for column in columns
                    )
                ):
                    raise HubError("invalid_rows", fatal=True)
            for row in page["rows"]:
                for column in source["columns"]:
                    if store.enqueue_backfill(sub, table, row, column, settings.max_pending_jobs):
                        accepted += 1
            with store.transaction():
                store.db.execute(
                    "INSERT INTO backfill_cursors VALUES (?,?,?,?) ON CONFLICT(subscription_id,source_table) DO UPDATE SET cursor=excluded.cursor,complete=excluded.complete",
                    (sub, table, next_cursor, int(next_cursor is None)),
                )
            if next_cursor is None:
                break
            cursor = next_cursor
    store.set_runtime("backfill", "complete")
    return {"observations": accepted, "status": "queued"}


async def recapture(settings, store, hub, capture_id: str) -> str:
    job = store.job(capture_id)
    if job["subscription_id"] != settings.subscription_id:
        raise HubError("wrong_subscription", fatal=True)
    source = job["source"]
    column = job["source_column"]
    store.bind_consumer(
        settings.hub_url, settings.subscription_id, settings.capture_table, settings.artifact_prefix
    )
    subscription = await hub.subscription()
    if subscription["state"] != "active" or not any(
        s["table"] == source["table"] and column in s["columns"] for s in subscription["sources"]
    ):
        raise HubError("source_not_authorized", fatal=True)
    rows = await hub.rows(
        source["table"],
        list(dict.fromkeys(["id", "updated_at", "hub_at", "deleted_at", column])),
        where={"id": source["row_id"]},
    )
    if len(rows["rows"]) != 1 or rows["rows"][0].get("id") != source["row_id"]:
        raise HubError("source_unavailable", fatal=True)
    result = store.enqueue_backfill(
        settings.subscription_id,
        source["table"],
        rows["rows"][0],
        column,
        settings.max_pending_jobs,
        observation_id=str(uuid.uuid4()),
    )
    if result is None:
        raise HubError("source_unavailable", fatal=True)
    return result
