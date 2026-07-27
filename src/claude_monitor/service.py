"""Production ASGI application running both ingestion planes."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.applications import Starlette

from .anthropic_client import AnthropicClient
from .config import Config
from .notion_client import NotionClient
from .otlp import Settings, create_app
from .retention import RetentionWorker
from .state import State
from .sync import activities, chats, directory
from .writer import Writer

LOG = logging.getLogger(__name__)


def _poll_once(config: Config, job: str) -> None:
    """Run one poll in a worker thread with its own SQLite connection."""
    state = State(config.state_db_path)
    try:
        client = AnthropicClient(config.compliance_access_key, config.compliance_base_url)
        notion = NotionClient(config.notion_token, config.notion_rate_limit_rps)
        if job == "chats":
            chats.sync(client, notion, state, config)
        elif job == "activities":
            activities.sync(client, notion, state, config)
        elif job == "directory":
            directory.sync(client, notion, state, config)
        else:
            RetentionWorker(notion, state, config).run_once()
    finally:
        state.connection.close()


async def _poll(job: str, interval: float, config: Config) -> None:
    while True:
        try:
            await asyncio.to_thread(_poll_once, config, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("%s background poll failed", job)
        await asyncio.sleep(interval)


def build_app() -> Starlette:
    """Build the deployable application and fail closed on incomplete configuration."""
    config = Config.from_env()
    otlp_app = create_app(Settings.from_env())
    original_lifespan = otlp_app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Validate the destination before accepting traffic or pulling private content.
        notion = NotionClient(config.notion_token, config.notion_rate_limit_rps)
        state = State(config.state_db_path)
        try:
            Writer(notion, state, config).validate_schema()
        finally:
            state.connection.close()
        async with original_lifespan(app):
            access = AnthropicClient(config.compliance_access_key, config.compliance_base_url).compliance_check()
            if not access.ok:
                LOG.error("Transcript synchronization disabled: %s. OTLP activity ingestion remains available.", access.message)
            intervals = {"chats": config.chat_poll_interval,
                         "activities": config.activity_poll_interval,
                         "directory": config.directory_poll_interval,
                         "retention": config.retention_interval}
            tasks = [asyncio.create_task(_poll(job, interval, config), name=f"poll-{job}")
                     for job, interval in intervals.items() if job != "chats" or access.ok]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    otlp_app.router.lifespan_context = lifespan
    return otlp_app


class _LazyApp:
    _app: Starlette | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = build_app()
        await self._app(scope, receive, send)


app = _LazyApp()
