"""Outbound intake and one recoverable capture worker."""

import asyncio
import random
import shutil
from pathlib import Path

from .capture import Outcome, capture
from .client import HubError
from .config import Settings
from .publication import publish_attempt
from .state import Store

RETRY_CAPTURE = {"timeout", "browser_error", "http_error", "storage_error"}


class Runner:
    def __init__(
        self, settings: Settings, store: Store, hub, *, capture_fn=capture, sleep=asyncio.sleep
    ):
        self.settings, self.store, self.hub = settings, store, hub
        self.capture_fn, self.sleep = capture_fn, sleep

    async def intake_once(self, wait: int = 30) -> bool:
        subscription = self.settings.subscription_id
        pending = self.store.pending_ack(subscription)
        if pending is None:
            body = await self.hub.poll(wait=wait)
            if body["subscription_id"] != subscription:
                raise HubError("invalid_delivery", fatal=True)
            self.store.accept_batch(body, self.settings.max_pending_jobs)
            pending = self.store.pending_ack(subscription)
        if pending is None:
            return False
        seq = await self.hub.ack(pending["delivery_id"])
        if seq != pending["through_seq"]:
            raise HubError("invalid_acknowledgment", fatal=True)
        self.store.acknowledge(subscription, pending["delivery_id"])
        return True

    async def process_one(self) -> bool:
        attempt = self.store.pending_attempt()
        if attempt is None:
            job = self.store.next_job()
            if job is None:
                return False
            attempt = self.store.begin_attempt(job["capture_id"])
        job = self.store.job(attempt["capture_id"])
        directory = self.settings.state_dir / "spool" / attempt["id"]
        if attempt["outcome"] is not None:
            outcome = Outcome.model_validate(attempt["outcome"])
        elif directory.exists() or directory.is_symlink():
            outcome = self.recover(directory)
            self.store.save_outcome(attempt["id"], outcome.model_dump())
        else:
            # The renderer receives no hub client or credential, even through config.
            capture_settings = self.settings.model_copy(
                update={"hub_token": None, "credential_command": None}
            )
            outcome = await self.capture_fn(job["url"], directory, capture_settings)
            self.store.save_outcome(attempt["id"], outcome.model_dump())
        await publish_attempt(self.hub, self.settings, job, attempt, outcome)
        self.store.finish_attempt(
            attempt["id"], retry=outcome.status in RETRY_CAPTURE and attempt["number"] < 3
        )
        if directory.exists():
            shutil.rmtree(directory)
        return True

    @staticmethod
    def recover(directory: Path) -> Outcome:
        manifest = directory / "capture.json"
        try:
            if (
                directory.is_symlink()
                or directory.parent.is_symlink()
                or manifest.is_symlink()
                or manifest.stat().st_size > 65_536
            ):
                raise ValueError()
            return Outcome.model_validate_json(manifest.read_bytes())
        except (OSError, ValueError):
            raise HubError("invalid_staged_manifest", fatal=True) from None

    async def loop(self, component: str, action) -> None:
        backoff = 1
        while True:
            try:
                worked = await action()
                self.store.set_runtime(component, "ready" if worked else "waiting")
                backoff = 1
                if not worked:
                    await self.sleep(1)
            except HubError as error:
                self.store.set_runtime(component, error.code)
                if error.fatal:
                    raise
                delay = (
                    min(3600, max(1, error.retry_after))
                    if error.retry_after is not None
                    else random.uniform(1, backoff)
                )
                await self.sleep(delay)
                backoff = min(60, backoff * 2)
            except ValueError as error:
                if str(error) != "queue_capacity":
                    self.store.set_runtime(component, "local_state_conflict")
                    raise HubError("local_state_conflict", fatal=True) from None
                self.store.set_runtime(component, "queue_full")
                await self.sleep(1)

    async def run(self) -> None:
        with self.store.runner_lock():
            tasks = [
                asyncio.create_task(self.loop("intake", self.intake_once)),
                asyncio.create_task(self.loop("worker", self.process_one)),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
