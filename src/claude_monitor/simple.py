"""The simple version: Claude Office Agents -> Notion. No database.

One endpoint. Claude posts an event, this writes a row to Notion, done.

No Postgres, no SQLite, no cron jobs, no queue, no state to lose. Duplicates
are avoided by asking Notion whether the Event ID is already there, which costs
one extra API call per event and removes the need for any local index.

Configuration is three variables:

    OTLP_SHARED_SECRET   the token you put in Claude's "OTLP headers" box
    NOTION_TOKEN         your Notion integration token
    NOTION_DS_ACTIVITY   the Agent Activity data source id

What this receives is Office Agents telemetry: who used Claude, in which app,
when, and how much. Claude does not send conversation text to any webhook, so
none arrives here. See docs/DESIGN.md section 1.

Trade-off versus `serverless.py`: with no durable queue, an event that arrives
while Notion is unreachable is lost rather than retried later. The OTLP exporter
retries on a 5xx, which covers the common case.
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

from .notion_client import NotionClient, date, number, select, text, title
from .otlp import JSON_TYPES, PROTO_TYPES, OtlpError, _decode, normalize
from .state import utcnow

LOG = logging.getLogger(__name__)


class Config:
    __slots__ = ("secret", "notion_token", "data_source", "allowed_origins", "max_body", "dedupe")

    def __init__(self) -> None:
        self.secret = os.environ.get("OTLP_SHARED_SECRET", "")
        self.notion_token = os.environ.get("NOTION_TOKEN", "")
        self.data_source = os.environ.get("NOTION_DS_ACTIVITY", "")
        missing = [name for name, value in (
            ("OTLP_SHARED_SECRET", self.secret),
            ("NOTION_TOKEN", self.notion_token),
            ("NOTION_DS_ACTIVITY", self.data_source)) if not value.strip()]
        if missing:
            raise RuntimeError("missing required environment variables: " + ", ".join(missing))
        if len(self.secret.encode()) < 32:
            raise RuntimeError("OTLP_SHARED_SECRET must be at least 32 bytes")
        self.allowed_origins = frozenset(
            x.strip() for x in os.environ.get("OTLP_ALLOWED_ORIGINS", "").split(",") if x.strip())
        self.max_body = int(os.environ.get("OTLP_MAX_BODY_BYTES", "4194304"))
        self.dedupe = os.environ.get("DEDUPE", "true").strip().lower() not in {"0", "false", "no"}


def _properties(record: dict[str, Any]) -> dict[str, Any]:
    email = str(record.get("actor_email") or "")
    surface = str(record.get("surface") or "claude.ai")
    props: dict[str, Any] = {
        "Event": title(f"{record['event_type']} · {email or 'unknown'}"),
        "Event ID": text(record["event_id"]),
        "Plane": select("otel-event"),
        "Event Type": text(record["event_type"]),
        "Actor Email": {"email": email if "@" in email else None},
        "Surface": select(surface),
        "Occurred At": date(record["occurred_at"]),
        "Prompt ID": text(record.get("prompt_id")),
        "IP Address": text(record.get("ip_address")),
        "User Agent": text(record.get("user_agent")),
        "Ingested At": date(utcnow()),
    }
    for key, name in (("cells_read", "Cells Read"), ("cells_written", "Cells Written"),
                      ("cells_copied", "Cells Copied")):
        if isinstance(record.get(key), (int, float)):
            props[name] = number(record[key])
    return props


def _write(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    notion = NotionClient(config.notion_token)
    created = skipped = 0
    for record in records:
        if config.dedupe:
            # Notion itself is the dedupe index, so there is nothing to persist
            # locally. One query per event; fine at Office Agents volumes.
            existing = notion.query_data_source(config.data_source, filter={
                "property": "Event ID", "rich_text": {"equals": record["event_id"]}})
            if existing.get("results"):
                skipped += 1
                continue
        notion.create_page(config.data_source, _properties(record))
        created += 1
    return {"created": created, "skipped": skipped}


def _cors(response: Response, origin: str | None, config: Config) -> Response:
    if origin and origin in config.allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


async def ingest(request: Request) -> Response:
    config: Config = request.app.state.config
    origin = request.headers.get("origin")
    expected = f"Bearer {config.secret}"
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
        return _cors(JSONResponse({"error": "unauthorized"}, status_code=401), origin, config)

    body = await request.body()
    if len(body) > config.max_body:
        return _cors(JSONResponse({"error": "request body is too large"}, status_code=413), origin, config)
    try:
        records = normalize(_decode(body, request.headers.get("content-type", "")))
    except OtlpError as exc:
        status = 415 if str(exc) == "unsupported Content-Type" else 400
        return _cors(JSONResponse({"error": str(exc)}, status_code=status), origin, config)

    try:
        result = await run_in_threadpool(_write, records, config)
    except Exception:
        # 5xx so the OTLP exporter retries; there is no queue to fall back on.
        LOG.exception("Notion write failed")
        return _cors(JSONResponse({"error": "notion unavailable"}, status_code=503), origin, config)
    return _cors(JSONResponse({"accepted": len(records), **result}, status_code=202), origin, config)


async def options(request: Request) -> Response:
    config: Config = request.app.state.config
    origin = request.headers.get("origin")
    if origin not in config.allowed_origins:
        return Response(status_code=403)
    response = Response(status_code=204)
    response.headers.update({
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "86400", "Vary": "Origin"})
    return response


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "accepts": sorted(PROTO_TYPES | JSON_TYPES)})


def create_app(config: Config | None = None) -> Starlette:
    resolved = config or Config()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        app.state.config = resolved
        yield

    return Starlette(routes=[
        Route("/v1/logs", ingest, methods=["POST"]),
        Route("/v1/logs", options, methods=["OPTIONS"]),
        Route("/health", health),
    ], lifespan=lifespan)


class _LazyApp:
    _app: Starlette | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
