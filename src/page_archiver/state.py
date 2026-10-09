"""Durable intake. The ACK receipt is committed with the work it acknowledges."""

import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Revision(Model):
    updated_at: str | None
    hub_at: str | None


class Source(Model):
    table: str = Field(min_length=1, max_length=200)
    row_id: str = Field(min_length=1, max_length=200)
    before_revision: Revision | None
    after_revision: Revision | None


class Change(Model):
    column: str = Field(min_length=1, max_length=200)
    old_value: str | None
    new_value: str | None


class Event(Model):
    id: str = Field(min_length=1, max_length=200)
    seq: str = Field(pattern=r"^[1-9][0-9]*$")
    operation: str = Field(pattern=r"^(insert|update|delete)$")
    recorded_at: str
    source: Source
    changes: list[Change]

    @field_validator("changes")
    @classmethod
    def unique_columns(cls, changes: list[Change]) -> list[Change]:
        if len({change.column for change in changes}) != len(changes):
            raise ValueError("duplicate event column")
        return changes


class Batch(Model):
    subscription_id: str = Field(min_length=1, max_length=200)
    delivery_id: str | None = Field(max_length=200)
    through_seq: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    events: list[Event] = Field(max_length=100)

    @model_validator(mode="after")
    def ordered(self):
        if not self.events:
            if self.delivery_id is not None:
                raise ValueError("empty batch cannot have a delivery ID")
            return self
        sequence = [int(event.seq) for event in self.events]
        if (
            not self.delivery_id
            or sequence != sorted(set(sequence))
            or self.through_seq != self.events[-1].seq
            or len({event.id for event in self.events}) != len(self.events)
        ):
            raise ValueError("invalid delivery ordering")
        return self


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / "inbox.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS events (
                subscription_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (subscription_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS pending_acks (
                subscription_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL,
                through_seq TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                capture_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, event_id TEXT NOT NULL,
                source_column TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                UNIQUE (subscription_id, event_id, source_column)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, capture_id TEXT NOT NULL, number INTEGER NOT NULL,
                started_at TEXT NOT NULL, outcome TEXT, published_at TEXT,
                UNIQUE(capture_id,number)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts((1))
                WHERE published_at IS NULL;
            CREATE TABLE IF NOT EXISTS runtime_status(component TEXT PRIMARY KEY, code TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumer_identity (id INTEGER PRIMARY KEY CHECK(id=1), configuration TEXT NOT NULL);
            COMMIT;
        """)
        with self.transaction():
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
            if "priority" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
            self.db.execute("PRAGMA user_version=2")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def runner_lock(self):
        fd = os.open(self.directory / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("runner_already_active") from None
            yield
        finally:
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def accept_batch(self, body: dict, max_pending_jobs: int = 10_000) -> None:
        batch = Batch.model_validate(body)
        payload = encoded(batch.model_dump())
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("batch exceeds delivery limit")
        if not batch.events:
            return
        with self.transaction():
            pending = self.db.execute(
                "SELECT delivery_id,payload FROM pending_acks WHERE subscription_id=?",
                (batch.subscription_id,),
            ).fetchone()
            if pending:
                if pending["delivery_id"] != batch.delivery_id:
                    raise ValueError("another delivery is pending")
                if pending["payload"] != payload:
                    raise ValueError("delivery payload conflict")
                return
            added = 0
            for event in batch.events:
                event_json = encoded(event.model_dump())
                old = self.db.execute(
                    "SELECT payload FROM events WHERE subscription_id=? AND event_id=?",
                    (batch.subscription_id, event.id),
                ).fetchone()
                if old:
                    if old["payload"] != event_json:
                        raise ValueError("event payload conflict")
                    continue
                self.db.execute(
                    "INSERT INTO events VALUES (?,?,?)",
                    (batch.subscription_id, event.id, event_json),
                )
                for change in event.changes:
                    if event.operation == "delete" or not change.new_value:
                        continue
                    added += 1
                    identity = encoded([batch.subscription_id, event.id, change.column])
                    self.db.execute(
                        "INSERT INTO jobs(capture_id,subscription_id,event_id,source_column,url) VALUES (?,?,?,?,?)",
                        (
                            str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                            batch.subscription_id,
                            event.id,
                            change.column,
                            change.new_value,
                        ),
                    )
            if added > max_pending_jobs:
                raise ValueError("delivery_exceeds_queue_capacity")
            self._check_capacity(max_pending_jobs)
            self.db.execute(
                "INSERT INTO pending_acks VALUES (?,?,?,?)",
                (batch.subscription_id, batch.delivery_id, batch.through_seq, payload),
            )

    def pending_ack(self, subscription_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT delivery_id,through_seq FROM pending_acks WHERE subscription_id=?",
            (subscription_id,),
        ).fetchone()
        return dict(row) if row else None

    def acknowledge(self, subscription_id: str, delivery_id: str) -> None:
        with self.transaction():
            result = self.db.execute(
                "DELETE FROM pending_acks WHERE subscription_id=? AND delivery_id=?",
                (subscription_id, delivery_id),
            )
            if result.rowcount != 1:
                raise ValueError("no matching pending acknowledgment")

    def jobs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY rowid")]

    def status(self) -> dict:
        return {
            "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
            "queued": self.db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[
                0
            ],
            "pending_acks": self.db.execute("SELECT count(*) FROM pending_acks").fetchone()[0],
            **{
                state: self.db.execute(
                    "SELECT count(*) FROM jobs WHERE state=?", (state,)
                ).fetchone()[0]
                for state in ("active", "succeeded", "partial", "failed")
            },
            "runtime": {
                row["component"]: row["code"]
                for row in self.db.execute("SELECT * FROM runtime_status")
            },
        }

    def _check_capacity(self, maximum: int) -> None:
        count = self.db.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','active')"
        ).fetchone()[0]
        if count > maximum:
            raise ValueError("queue_capacity")

    def job(self, capture_id: str) -> dict:
        row = self.db.execute(
            "SELECT j.*,e.payload FROM jobs j JOIN events e USING(subscription_id,event_id) WHERE j.capture_id=?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown_job")
        job = dict(row)
        job["event"] = json.loads(job.pop("payload"))
        job["source"] = job["event"]["source"]
        return job

    def next_job(self) -> dict | None:
        row = self.db.execute(
            "SELECT capture_id FROM jobs WHERE state='queued' ORDER BY priority DESC,rowid LIMIT 1"
        ).fetchone()
        return self.job(row[0]) if row else None

    @staticmethod
    def _attempt(row) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["outcome"] = json.loads(value["outcome"]) if value["outcome"] else None
        return value

    def pending_attempt(self) -> dict | None:
        return self._attempt(
            self.db.execute("SELECT * FROM attempts WHERE published_at IS NULL").fetchone()
        )

    def begin_attempt(self, capture_id: str) -> dict:
        with self.transaction():
            pending = self.pending_attempt()
            if pending:
                if pending["capture_id"] != capture_id:
                    raise ValueError("attempt_already_active")
                return pending
            job = self.job(capture_id)
            if job["state"] != "queued":
                raise ValueError("job_not_queued")
            number = self.db.execute(
                "SELECT coalesce(max(number),0)+1 FROM attempts WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
            attempt_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO attempts(id,capture_id,number,started_at) VALUES (?,?,?,?)",
                (attempt_id, capture_id, number, datetime.now(UTC).isoformat()),
            )
            self.db.execute("UPDATE jobs SET state='active' WHERE capture_id=?", (capture_id,))
            return self._attempt(
                self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            )

    def save_outcome(self, attempt_id: str, outcome: dict) -> None:
        payload = encoded(outcome)
        with self.transaction():
            row = self.db.execute(
                "SELECT outcome,published_at FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown_attempt")
            if row["outcome"] is not None:
                if row["outcome"] != payload:
                    raise ValueError("outcome_conflict")
                return
            if row["published_at"]:
                raise ValueError("attempt_already_published")
            self.db.execute("UPDATE attempts SET outcome=? WHERE id=?", (payload, attempt_id))

    def finish_attempt(self, attempt_id: str, *, retry: bool = False) -> None:
        with self.transaction():
            attempt = self._attempt(
                self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            )
            if not attempt or not attempt["outcome"]:
                raise ValueError("outcome_required")
            if attempt["published_at"]:
                return
            state = (
                "succeeded"
                if attempt["outcome"]["status"] == "succeeded"
                else "partial"
                if attempt["outcome"]["status"] == "partial"
                and attempt["outcome"].get("captured_at")
                and attempt["outcome"].get("html")
                and attempt["outcome"].get("png")
                else "queued"
                if retry
                else "failed"
            )
            self.db.execute(
                "UPDATE attempts SET published_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), attempt_id),
            )
            self.db.execute(
                "UPDATE jobs SET state=? WHERE capture_id=?", (state, attempt["capture_id"])
            )

    def enqueue_backfill(
        self,
        subscription_id: str,
        table: str,
        row: dict,
        column: str,
        max_pending_jobs: int = 10_000,
        *,
        observation_id: str | None = None,
    ) -> str | None:
        url = row.get(column)
        if row.get("deleted_at") or not url:
            return None
        revision = {"updated_at": row.get("updated_at"), "hub_at": row.get("hub_at")}
        identity = encoded([subscription_id, table, row["id"], column, url, revision])
        event_id = (
            ("recapture:" + observation_id)
            if observation_id
            else "backfill:" + str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        )
        capture_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, encoded([subscription_id, event_id, column]))
        )
        with self.transaction():
            if self.db.execute("SELECT 1 FROM jobs WHERE capture_id=?", (capture_id,)).fetchone():
                return capture_id
            event = Event(
                id=event_id,
                seq="1",
                operation="insert",
                recorded_at=datetime.now(UTC).isoformat(),
                source=Source(
                    table=table,
                    row_id=row["id"],
                    before_revision=None,
                    after_revision=Revision(**revision),
                ),
                changes=[Change(column=column, old_value=None, new_value=url)],
            )
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (subscription_id, event_id, encoded(event.model_dump())),
            )
            self.db.execute(
                "INSERT INTO jobs(capture_id,subscription_id,event_id,source_column,url,priority) VALUES (?,?,?,?,?,0)",
                (capture_id, subscription_id, event_id, column, url),
            )
            self._check_capacity(max_pending_jobs)
        return capture_id

    def bind_consumer(self, hub_url, subscription_id, capture_table, artifact_prefix) -> None:
        identity = encoded([hub_url, subscription_id, capture_table, artifact_prefix])
        with self.transaction():
            previous = self.db.execute(
                "SELECT configuration FROM consumer_identity WHERE id=1"
            ).fetchone()
            if previous and previous[0] != identity:
                raise ValueError("consumer_configuration_changed")
            self.db.execute("INSERT OR IGNORE INTO consumer_identity VALUES (1,?)", (identity,))

    def move_hub(self, hub_url, subscription_id, capture_table, artifact_prefix) -> None:
        """Rebind durable work to a new URL of the same hub; every other part must match."""
        with self.transaction():
            previous = self.db.execute(
                "SELECT configuration FROM consumer_identity WHERE id=1"
            ).fetchone()
            if previous and json.loads(previous[0])[1:] != [
                subscription_id,
                capture_table,
                artifact_prefix,
            ]:
                raise ValueError("consumer_configuration_changed")
            self.db.execute(
                "INSERT OR REPLACE INTO consumer_identity VALUES (1,?)",
                (encoded([hub_url, subscription_id, capture_table, artifact_prefix]),),
            )

    def set_runtime(self, component: str, code: str) -> None:
        if (
            component not in {"intake", "worker", "backfill"}
            or not code.replace("_", "").isalnum()
            or len(code) > 64
        ):
            raise ValueError("invalid_runtime_status")
        with self.transaction():
            self.db.execute(
                "INSERT INTO runtime_status VALUES (?,?) ON CONFLICT(component) DO UPDATE SET code=excluded.code",
                (component, code),
            )
