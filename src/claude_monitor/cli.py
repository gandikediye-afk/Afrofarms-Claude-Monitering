"""The ``claude-monitor`` command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import time

from .anthropic_client import AnthropicClient
from .config import Config, ConfigError, NotionConfig
from .notion_client import NotionClient, NotionError
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
    notion_commands = notion.add_subparsers(dest="notion_command", required=True)
    notion_commands.add_parser("check")
    discover = notion_commands.add_parser("discover", help="discover and validate Notion data sources")
    discover.add_argument(
        "locations", nargs="+", metavar="PARENT_PAGE_ID_OR_DATABASE_URL",
        help="a monitoring parent page ID, or one or more Notion database URLs",
    )
    return result


_DISCOVERY_DATABASES = {
    "Team Members": ("Members", "NOTION_DS_MEMBERS"),
    "Claude Projects": ("Projects", "NOTION_DS_PROJECTS"),
    "Sync Runs": ("Sync Runs", "NOTION_DS_SYNC_RUNS"),
    "Conversations": ("Conversations", "NOTION_DS_CONVERSATIONS"),
    "Agent Activity": ("Agent Activity", "NOTION_DS_ACTIVITY"),
    "Messages": ("Messages", "NOTION_DS_MESSAGES"),
}
_NOTION_ID = re.compile(r"(?i)([0-9a-f]{32})(?:[?#/]|$)")


def _notion_id(value: str) -> str:
    """Extract a Notion object ID without retaining the input URL."""
    compact = value.strip().replace("-", "")
    if re.fullmatch(r"(?i)[0-9a-f]{32}", compact):
        return compact
    match = _NOTION_ID.search(value)
    if not match:
        raise ConfigError(f"not a Notion page ID or database URL: {value!r}")
    return match.group(1)


def _database_title(database: dict[str, object]) -> str:
    return "".join(
        str(part.get("plain_text", ""))
        for part in database.get("title", [])
        if isinstance(part, dict)
    )


def _discover(client: NotionClient, locations: list[str]) -> tuple[str | None, dict[str, str]]:
    parent_id: str | None = None
    databases: list[dict[str, object]] = []
    for location in locations:
        object_id = _notion_id(location)
        if location.lower().startswith(("http://", "https://")):
            databases.append(client.database(object_id))
        else:
            if parent_id is not None:
                raise ConfigError("only one monitoring parent-page ID may be supplied")
            parent_id = object_id
            databases.extend(client.child_databases(object_id))

    matches: dict[str, list[dict[str, object]]] = {title: [] for title in _DISCOVERY_DATABASES}
    for database in databases:
        title = _database_title(database)
        if title in matches:
            matches[title].append(database)
    duplicate = next((title for title, found in matches.items() if len(found) > 1), None)
    if duplicate:
        raise ConfigError(f"duplicate Notion database title {duplicate!r}; titles must be unique")

    discovered: dict[str, str] = {}
    for title, (_label, variable) in _DISCOVERY_DATABASES.items():
        if not matches[title]:
            if title == "Messages":
                continue
            raise ConfigError(f"required Notion database {title!r} was not found")
        sources = matches[title][0].get("data_sources") or []
        if len(sources) != 1 or not isinstance(sources[0], dict) or not sources[0].get("id"):
            raise ConfigError(f"Notion database {title!r} must have exactly one current data source")
        discovered[variable] = str(sources[0]["id"])
    return parent_id, discovered


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
        if args.notion_command == "discover":
            token = os.environ.get("NOTION_TOKEN", "").strip()
            if not token:
                parser().error("missing required environment variable: NOTION_TOKEN")
            try:
                rate = float(os.environ.get("NOTION_RATE_LIMIT_RPS", "2.5"))
                if rate <= 0:
                    raise ValueError
                parent_id, discovered = _discover(NotionClient(token, rate), args.locations)
            except (ConfigError, NotionError, ValueError) as exc:
                parser().error(str(exc).replace(token, "[REDACTED]"))
            print("# Discovered by claude-monitor; NOTION_TOKEN is never written")
            print("NOTION_TOKEN=[REDACTED]")
            if parent_id:
                print(f"NOTION_PARENT_PAGE_ID={parent_id}")
            for _title, (label, variable) in _DISCOVERY_DATABASES.items():
                if variable in discovered:
                    print(f"{variable}={discovered[variable]}")
            sources = {
                label: discovered[variable]
                for _title, (label, variable) in _DISCOVERY_DATABASES.items()
                if variable in discovered
            }
            results = check_notion_config(NotionClient(token, rate), sources)
            for name, error in results:
                print(f"{'OK' if error is None else 'ERROR'}  {name}" + (f": {error}" if error else ""))
            return 1 if any(error for _, error in results) else 0
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
