"""ASGI application for serverless hosts (Vercel, Lambda, Cloud Run jobs).

Differs from `service.py` in exactly two ways, both forced by the platform:

* **No background loops.** `service.py` runs `while True: ... await sleep(interval)`
  tasks inside the ASGI lifespan. A serverless function is frozen once its
  response is sent, so those never tick. The platform scheduler calls the
  `/api/cron/{job}` routes instead, one bounded run per invocation.
* **No in-process drain.** `otlp.create_app` spawns a worker that writes queued
  OTLP events to Notion after the response. That also never runs here, so
  `/v1/logs` durably enqueues and returns 202, and `/api/cron/drain` performs
  the Notion writes on the next tick. The durable queue already existed for
  exactly this hand-off.

`DATABASE_URL` must point at Postgres. On an ephemeral filesystem SQLite loses
the chat_id -> notion_page_id index on every cold start, and the writer then
creates a second Notion page for every chat it has already mirrored.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import Config, ConfigError
from .db import is_postgres_dsn
from .otlp import Settings, health, ingest, options, ready
from .otlp import _drain  # noqa: F401 - reused as the "drain" cron job
from .state import State

LOG = logging.getLogger(__name__)

JOBS = ("chats", "activities", "directory", "retention", "drain")


def _run_job(job: str) -> dict[str, Any]:
    """Run one bounded job. Imports are local so /v1/logs never pays for them."""
    if job == "drain":
        _drain(Settings.from_env())
        return {"job": job, "ok": True}

    from .anthropic_client import AnthropicClient
    from .notion_client import NotionClient
    from .retention import RetentionWorker
    from .sync import activities, chats, directory

    config = Config.from_env()
    state = State(config.state_target)
    try:
        notion = NotionClient(config.notion_token, config.notion_rate_limit_rps)
        client = AnthropicClient(config.compliance_access_key, config.compliance_base_url)
        if job == "chats":
            run = chats.sync(client, notion, state, config)
        elif job == "activities":
            run = activities.sync(client, notion, state, config)
        elif job == "directory":
            run = directory.sync(client, notion, state, config)
        else:
            RetentionWorker(notion, state, config).run_once()
            return {"job": job, "ok": True}
        return {"job": job, "ok": True, "records": run.records, "pages": run.pages,
                "created": run.created, "updated": run.updated, "unchanged": run.unchanged}
    finally:
        state.connection.close()


def _authorized(request: Request) -> bool:
    """Vercel Cron sends `Authorization: Bearer $CRON_SECRET`."""
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return False
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode())


async def cron(request: Request) -> Response:
    job = request.path_params.get("job", "")
    if job not in JOBS:
        return JSONResponse({"error": f"unknown job; expected one of {', '.join(JOBS)}"}, status_code=404)
    if not _authorized(request):
        # Same response for a missing CRON_SECRET and a wrong one: an unconfigured
        # deployment must not advertise that its cron endpoints are unprotected.
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = await run_in_threadpool(_run_job, job)
    except Exception as exc:
        LOG.exception("cron job %s failed", job)
        return JSONResponse({"job": job, "ok": False, "error": type(exc).__name__}, status_code=500)
    return JSONResponse(result)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    if not is_postgres_dsn(settings.state_target):
        raise ConfigError(
            "serverless deployments require DATABASE_URL to point at Postgres; "
            "an ephemeral filesystem loses the idempotency index and duplicates "
            "every Notion page on each cold start"
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = State(settings.state_target)
        app.state.wakeup = _NullEvent()  # nothing to wake: the drain runs on cron
        try:
            yield
        finally:
            app.state.db.connection.close()

    return Starlette(routes=[
        Route("/v1/logs", ingest, methods=["POST"]),
        Route("/v1/logs", options, methods=["OPTIONS"]),
        Route("/health", health),
        Route("/ready", ready),
        Route("/api/cron/{job}", cron, methods=["GET", "POST"]),
    ], lifespan=lifespan)


class _NullEvent:
    """`ingest` signals a worker that does not exist in this deployment."""

    def set(self) -> None: return None
    def clear(self) -> None: return None


class _LazyApp:
    _app: Starlette | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
