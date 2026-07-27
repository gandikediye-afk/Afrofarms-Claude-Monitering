"""Hardened OTLP/HTTP log ingestion, deliberately separate from chat syncing.

Run with, for example, ``uvicorn claude_monitor.otlp:app``.  A reverse proxy must
terminate TLS and pass an ASGI scope whose scheme is ``https``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .notion_client import NotionClient, date, number, relation, select, text, title
from .state import State, utcnow

LOG = logging.getLogger(__name__)
PLANE = "otel"
PROTO_TYPES = {"application/x-protobuf", "application/protobuf"}


class OtlpError(ValueError):
    """A safe client-facing OTLP validation error."""


@dataclass(frozen=True, slots=True)
class Settings:
    shared_secret: str
    ingest_header: str
    max_body_bytes: int
    allowed_origins: frozenset[str]
    state_db_path: Path
    notion_token: str | None
    notion_ds_activity: str | None
    notion_rate_limit_rps: float

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("OTLP_SHARED_SECRET", "")
        if not secret:
            raise RuntimeError("OTLP_SHARED_SECRET is required")
        if len(secret.encode("utf-8")) < 32:
            raise RuntimeError("OTLP_SHARED_SECRET must contain at least 32 bytes")
        header = "authorization"
        try:
            maximum = int(os.environ.get("OTLP_MAX_BODY_BYTES", "1048576"))
            rps = float(os.environ.get("NOTION_RATE_LIMIT_RPS", "2.5"))
        except ValueError as exc:
            raise RuntimeError("OTLP_MAX_BODY_BYTES and NOTION_RATE_LIMIT_RPS must be numeric") from exc
        if maximum < 1 or rps <= 0:
            raise RuntimeError("OTLP_MAX_BODY_BYTES and NOTION_RATE_LIMIT_RPS must be positive")
        origins = frozenset(x.strip() for x in os.environ.get("OTLP_ALLOWED_ORIGINS", "").split(",") if x.strip())
        return cls(secret, header, maximum, origins,
                   Path(os.environ.get("STATE_DB_PATH", "/var/lib/claude-monitor/state.db")),
                   os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_DS_ACTIVITY"), rps)


def _value(value: Any) -> Any:
    """Decode an OTLP AnyValue JSON representation into ordinary JSON values."""
    if not isinstance(value, dict):
        return value
    names = ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue")
    for name in names:
        if name in value:
            raw = value[name]
            if name == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return raw
            if name == "doubleValue":
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return raw
            return raw
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {str(item.get("key")): _value(item.get("value")) for item in value["kvlistValue"].get("values", [])}
    return None


def _attrs(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        raise OtlpError("attributes must be an array")
    result: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or "value" not in item:
            raise OtlpError("invalid attribute")
        result[item["key"]] = _value(item["value"])
    return result


def _iso(nanos: Any) -> str:
    try:
        value = int(nanos)
    except (TypeError, ValueError) as exc:
        raise OtlpError("log record has an invalid timestamp") from exc
    if value <= 0:
        raise OtlpError("log record timestamp is required")
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat()


def normalize(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and flatten OTLP logs without retaining body/transcript content."""
    resource_logs = envelope.get("resourceLogs")
    if not isinstance(resource_logs, list) or not resource_logs:
        raise OtlpError("resourceLogs must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for resource_group in resource_logs:
        if not isinstance(resource_group, dict):
            raise OtlpError("invalid resourceLogs entry")
        resource = _attrs((resource_group.get("resource") or {}).get("attributes", []))
        scopes = resource_group.get("scopeLogs")
        if not isinstance(scopes, list):
            raise OtlpError("scopeLogs must be an array")
        for scope_group in scopes:
            if not isinstance(scope_group, dict) or not isinstance(scope_group.get("logRecords"), list):
                raise OtlpError("invalid scopeLogs entry")
            scope = scope_group.get("scope") or {}
            for record in scope_group["logRecords"]:
                if not isinstance(record, dict):
                    raise OtlpError("invalid log record")
                attributes = {**resource, **_attrs(record.get("attributes", []))}
                occurred = _iso(record.get("timeUnixNano") or record.get("observedTimeUnixNano"))
                event_type = str(attributes.get("event.name") or attributes.get("event.type") or
                                 attributes.get("type") or record.get("eventName") or "unknown")
                member_id = attributes.get("user.id") or attributes.get("member.id") or attributes.get("enduser.id")
                email = attributes.get("user.email") or attributes.get("member.email") or attributes.get("enduser.email")
                surface = attributes.get("surface") or attributes.get("claude.surface") or attributes.get("service.name")
                known = {"event.name", "event.type", "type", "user.id", "member.id", "enduser.id",
                         "user.email", "member.email", "enduser.email", "surface", "claude.surface", "service.name",
                         "prompt.id", "conversation.id", "chat.id", "client.address", "ip.address", "user_agent.original",
                         "sheet.cells_read", "sheet.cells_written", "sheet.cells_copied", "event.id"}
                extra = {k: v for k, v in attributes.items() if k not in known}
                candidate = {"event_type": event_type, "occurred_at": occurred,
                             "prompt_id": attributes.get("prompt.id"), "member_id": member_id,
                             "actor_email": email, "surface": surface,
                             "conversation_id": attributes.get("conversation.id") or attributes.get("chat.id"),
                             "ip_address": attributes.get("client.address") or attributes.get("ip.address"),
                             "user_agent": attributes.get("user_agent.original"),
                             "cells_read": attributes.get("sheet.cells_read"),
                             "cells_written": attributes.get("sheet.cells_written"),
                             "cells_copied": attributes.get("sheet.cells_copied"),
                             "attributes": extra, "scope": scope.get("name")}
                canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                trace_span = (f"{record.get('traceId')}:{record.get('spanId')}"
                              if record.get("traceId") and record.get("spanId") else None)
                stable = attributes.get("event.id") or trace_span
                candidate["event_id"] = str(stable or hashlib.sha256(canonical.encode()).hexdigest())
                normalized.append(candidate)
    if not normalized:
        raise OtlpError("payload contains no log records")
    return normalized


async def _body(request: Request, maximum: int) -> bytes:
    length = request.headers.get("content-length")
    if length:
        # Parse inside the guard, compare outside it: OtlpError subclasses ValueError,
        # so raising the size error in here would be caught by our own except clause
        # and downgraded to "invalid Content-Length" (400 instead of 413).
        try:
            declared = int(length)
        except ValueError as exc:
            raise OtlpError("invalid Content-Length") from exc
        if declared > maximum:
            raise OtlpError("request body is too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise OtlpError("request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(body: bytes, content_type: str) -> Mapping[str, Any]:
    media_type = content_type.split(";", 1)[0].strip().lower()
    try:
        if media_type in PROTO_TYPES:
            message = ExportLogsServiceRequest.FromString(body)
            value = MessageToDict(message, preserving_proto_field_name=False)
        else:
            raise OtlpError("unsupported Content-Type")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise OtlpError("malformed OTLP payload") from exc
    if not isinstance(value, dict):
        raise OtlpError("OTLP envelope must be an object")
    return value


def _cors(response: Response, origin: str | None, settings: Settings) -> Response:
    if origin and origin in settings.allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


async def ingest(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if request.url.scheme != "https":
        return JSONResponse({"error": "HTTPS required"}, status_code=400)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.shared_secret}"
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _cors(JSONResponse({"error": "unauthorized"}, status_code=401), origin, settings)
    try:
        body = await _body(request, settings.max_body_bytes)
        records = normalize(_decode(body, request.headers.get("content-type", "")))
    except OtlpError as exc:
        status = 413 if str(exc) == "request body is too large" else 415 if str(exc) == "unsupported Content-Type" else 400
        return _cors(JSONResponse({"error": str(exc)}, status_code=status), origin, settings)
    state: State = request.app.state.db
    try:
        with state.transaction():
            for record in records:
                # Only normalized metadata enters durable storage; OTLP bodies are discarded.
                state.stage(PLANE, record["event_id"], record)
    except Exception:
        LOG.exception("OTLP durable enqueue failed")
        return JSONResponse({"error": "ingest unavailable"}, status_code=503)
    request.app.state.wakeup.set()
    return _cors(JSONResponse({"accepted": len(records)}, status_code=202), origin, settings)


async def options(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin not in settings.allowed_origins:
        return Response(status_code=403)
    response = Response(status_code=204)
    response.headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
                             "Access-Control-Allow-Headers": "Authorization, Content-Type",
                             "Access-Control-Max-Age": "86400", "Vary": "Origin"})
    return response


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> Response:
    try:
        request.app.state.db.connection.execute("SELECT 1").fetchone()
        configured = bool(request.app.state.settings.notion_token and request.app.state.settings.notion_ds_activity)
    except Exception:
        configured = False
    return JSONResponse({"status": "ready" if configured else "not_ready"}, status_code=200 if configured else 503)


def _properties(record: dict[str, Any], state: State) -> dict[str, Any]:
    member = state.object("user", str(record.get("member_id"))) if record.get("member_id") else None
    chat = state.chat(str(record.get("conversation_id"))) if record.get("conversation_id") else None
    email = str(record.get("actor_email") or "")
    props = {"Event": title(f"{record['event_type']} · {email}"), "Event ID": text(record["event_id"]),
             "Plane": select("otel-event"), "Event Type": text(record["event_type"]),
             "Actor": relation(member["notion_page_id"] if member else None),
             "Actor Email": {"email": email if "@" in email else None}, "Surface": select(str(record.get("surface") or "claude.ai")),
             "Occurred At": date(record["occurred_at"]), "Prompt ID": text(record.get("prompt_id")),
             "Conversation": relation(chat["notion_page_id"] if chat else None), "IP Address": text(record.get("ip_address")),
             "User Agent": text(record.get("user_agent")),
             "Attributes": text(json.dumps(record.get("attributes", {}), sort_keys=True, separators=(",", ":"))),
             "Ingested At": date(utcnow())}
    for key, prop in (("cells_read", "Cells Read"), ("cells_written", "Cells Written"), ("cells_copied", "Cells Copied")):
        if isinstance(record.get(key), (int, float)): props[prop] = number(record[key])
    return props


def _drain(settings: Settings) -> None:
    if not settings.notion_token or not settings.notion_ds_activity:
        return
    state = State(settings.state_db_path)
    try:
        notion = NotionClient(settings.notion_token, settings.notion_rate_limit_rps)
        for item in state.queued(PLANE):
            record = json.loads(item["payload"]); event_id = item["object_id"]
            digest = hashlib.sha256(item["payload"].encode()).hexdigest()
            existing = state.object("activity", event_id)
            if existing and existing["content_hash"] == digest:
                state.complete_item(PLANE, event_id); continue
            props = _properties(record, state)
            if existing:
                notion.update_page(existing["notion_page_id"], props); page_id = existing["notion_page_id"]
            else:
                page_id = notion.create_page(settings.notion_ds_activity, props)
            state.put_object("activity", event_id, page_id, digest)
            state.complete_item(PLANE, event_id)
    finally:
        state.connection.close()


async def _worker(app: Starlette) -> None:
    while True:
        await app.state.wakeup.wait(); app.state.wakeup.clear()
        try:
            await asyncio.to_thread(_drain, app.state.settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("OTLP asynchronous Notion write failed")
            await asyncio.sleep(2); app.state.wakeup.set()


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = State(settings.state_db_path)
        app.state.wakeup = asyncio.Event(); app.state.wakeup.set()
        worker = asyncio.create_task(_worker(app))
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            app.state.db.connection.close()

    return Starlette(routes=[Route("/v1/logs", ingest, methods=["POST"]), Route("/v1/logs", options, methods=["OPTIONS"]),
                             Route("/health", health), Route("/ready", ready)], lifespan=lifespan)


# Lazy ASGI wrapper keeps importing this module safe for tests and management commands.
class _LazyApp:
    _app: Starlette | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
