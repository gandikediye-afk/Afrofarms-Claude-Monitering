"""The ``claude-monitor`` command line interface."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .anthropic_client import AnthropicClient
from .config import Config, ConfigError, NotionConfig
from .notion_client import NotionClient
from .state import State
from .writer import Writer, check_notion_config
from .retention import RetentionWorker
from .sync import activities, chats, directory


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="claude-monitor", description="Mirror Claude compliance data to Notion")
    commands = result.add_subparsers(dest="command", required=True)
    directory_command = commands.add_parser("directory"); directory_sub = directory_command.add_subparsers(dest="directory_command", required=True); directory_sub.add_parser("sync")
    backfill = commands.add_parser("backfill"); backfill.add_argument("--dry-run", action="store_true", help="read and estimate without conversation writes")
    commands.add_parser("daemon")
    notion = commands.add_parser("notion")
    notion.add_subparsers(dest="notion_command", required=True).add_parser("check")
    return result


def _services(config: Config) -> tuple[AnthropicClient, NotionClient, State]:
    return (AnthropicClient(config.compliance_access_key, config.compliance_base_url),
            NotionClient(config.notion_token, config.notion_rate_limit_rps), State(config.state_db_path))


def _daemon(client: AnthropicClient, notion: NotionClient, state: State, config: Config) -> None:
    stopping = False
    def stop(*_: object) -> None:
        nonlocal stopping; stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    due = {"chats": 0.0, "activities": 0.0, "directory": 0.0, "retention": 0.0}
    while not stopping:
        now = time.monotonic()
        if now >= due["activities"]: activities.sync(client, notion, state, config); due["activities"] = now + config.activity_poll_interval
        if now >= due["chats"]: chats.sync(client, notion, state, config); due["chats"] = now + config.chat_poll_interval
        if now >= due["directory"]: directory.sync(client, notion, state, config); due["directory"] = now + config.directory_poll_interval
        if now >= due["retention"]: RetentionWorker(notion, state, config).run_once(); due["retention"] = now + config.retention_interval
        time.sleep(min(1.0, max(0.05, min(due.values()) - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "notion":
        try:
            notion_config = NotionConfig.from_env()
        except ConfigError as exc:
            parser().error(str(exc))
        results = check_notion_config(
            NotionClient(notion_config.token, notion_config.rate_limit_rps), notion_config.data_sources
        )
        for name, error in results:
            print(f"{'OK' if error is None else 'ERROR'}  {name}" + (f": {error}" if error else ""))
        return 1 if any(error for _, error in results) else 0
    try: config = Config.from_env()
    except ConfigError as exc:
        parser().error(str(exc))
    client, notion, state = _services(config)
    Writer(notion, state, config).validate_schema()
    if args.command == "directory": directory.sync(client, notion, state, config)
    elif args.command == "backfill":
        run = chats.sync(client, notion, state, config, backfill=True, dry_run=args.dry_run)
        if args.dry_run:
            blocks_estimate = run.records * 3
            print(f"Estimated chats: {run.records}; Notion calls: ~{run.records + (blocks_estimate + 99) // 100}; duration: ~{(run.records + (blocks_estimate + 99) // 100) / config.notion_rate_limit_rps:.0f}s")
    else: _daemon(client, notion, state, config)
    return 0


if __name__ == "__main__": raise SystemExit(main())
