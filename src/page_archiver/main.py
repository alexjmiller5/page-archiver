"""Installed command-line interface; configuration is resolved only when invoked."""

import argparse
import asyncio
import json
import sqlite3

from pydantic import ValidationError
from pydantic_settings import SettingsError
from pathlib import Path

from page_archiver.backfill import backfill, recapture
from page_archiver.client import HubClient, HubError
from page_archiver.retrieval import retrieve
from page_archiver.runner import Runner, retry_hub
from page_archiver.capture import capture
from page_archiver.config import Settings
from page_archiver.state import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="page-archiver", description="Retain web pages and screenshots"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show durable local work counts without contacting the hub")
    capture_parser = commands.add_parser(
        "capture", help="Capture one public page into a new directory"
    )
    capture_parser.add_argument("url")
    capture_parser.add_argument("--output", type=Path, required=True)
    commands.add_parser("watch", help="Run outbound intake and one capture worker")
    commands.add_parser("run-once", help="Accept available events and process one queued capture")
    commands.add_parser("backfill", help="Queue current URLs from subscription-selected columns")
    retry_parser = commands.add_parser(
        "retry", help="Queue a new observation of a capture's current source"
    )
    retry_parser.add_argument("capture_id")
    get_parser = commands.add_parser(
        "retrieve", help="Download a successful capture through file authorization"
    )
    get_parser.add_argument("attempt_id")
    get_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return execute(args)
    except (ValidationError, SettingsError):
        error, code = "invalid_configuration", 78
    except HubError as problem:
        error, code = problem.code, 78 if problem.fatal else 1
    except (ValueError, RuntimeError):
        error, code = "local_state_conflict", 78
    except (OSError, sqlite3.Error):
        error, code = "local_storage_error", 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps({"error": error}))
    return code


def execute(args) -> int:
    settings = Settings()
    if args.command == "capture":
        result = asyncio.run(capture(args.url, args.output.absolute(), settings))
        print(result.model_dump_json())
        return 0 if result.status == "succeeded" else 1
    if args.command == "status":
        with Store(settings.state_dir) as store:
            print(json.dumps(store.status()))
    else:
        print(json.dumps(asyncio.run(hub_command(args, settings))))
    return 0


async def hub_command(args, settings):
    async with HubClient(settings) as hub:
        if args.command == "retrieve":
            await hub.session()
            return await retrieve(hub, settings, args.attempt_id, args.output.absolute())
        with Store(settings.state_dir) as store:
            if args.command == "watch":
                await retry_hub(hub.session, store, "intake")
            else:
                await hub.session()
            if args.command == "backfill":
                return await backfill(settings, store, hub)
            if args.command == "retry":
                return {"capture_id": await recapture(settings, store, hub, args.capture_id)}
            if args.command == "watch":
                await retry_hub(hub.subscription, store, "intake")
            else:
                await hub.subscription()
            runner = Runner(settings, store, hub)
            if args.command == "watch":
                await runner.run()
            else:
                with store.runner_lock():
                    await runner.intake_once(wait=0)
                    await runner.process_one()
            return store.status()


if __name__ == "__main__":
    raise SystemExit(main())
