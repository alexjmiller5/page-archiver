"""Durable intake. The ACK receipt is committed with the work it acknowledges."""

import json
import os
import sqlite3
import uuid
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
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def accept_batch(self, body: dict) -> None:
        batch = Batch.model_validate(body)
        payload = encoded(batch.model_dump())
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("batch exceeds delivery limit")
        if not batch.events:
            return
        with self.db:
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
        with self.db:
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
        }
