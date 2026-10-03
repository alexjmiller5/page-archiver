"""Installed command-line interface; configuration is resolved only when invoked."""

import argparse
import asyncio
import json
from pathlib import Path

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
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "capture":
        result = asyncio.run(capture(args.url, args.output.absolute(), settings))
        print(result.model_dump_json())
        return 0 if result.status == "succeeded" else 1
    with Store(settings.state_dir) as store:
        print(json.dumps(store.status()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
