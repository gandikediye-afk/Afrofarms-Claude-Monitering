"""Small, dependency-free Notion API client.

The limiter is deliberately process-global: constructing one client per sync plane must not
multiply the integration's request rate.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from .normalizer import redact_structure


class NotionError(RuntimeError):
    pass


class SchemaError(NotionError):
    pass


class _TokenBucket:
    def __init__(self, rate: float):
        self.rate, self.capacity, self.tokens = rate, max(1.0, rate), max(1.0, rate)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            time.sleep(delay)


_buckets: dict[float, _TokenBucket] = {}
_buckets_lock = threading.Lock()


def _bucket(rate: float) -> _TokenBucket:
    with _buckets_lock:
        return _buckets.setdefault(rate, _TokenBucket(rate))


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class NotionClient:
    def __init__(self, token: str, rps: float = 2.5, *, opener: Callable[..., Any] = urllib.request.urlopen):
        if rps <= 0:
            raise ValueError("rps must be positive")
        self._token, self._limiter, self._opener = token, _bucket(rps), opener

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(6):
            self._limiter.acquire()
            request = urllib.request.Request(
                "https://api.notion.com/v1" + path, data=encoded, method=method,
                headers={"Authorization": f"Bearer {self._token}", "Notion-Version": "2025-09-03", "Content-Type": "application/json"},
            )
            try:
                with self._opener(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                # An ambiguous page-creation retry can create twins.  The durable writer
                # reconciles those operations by stable ID on its next pass instead.
                retry_safe = method != "POST" or path != "/pages" or exc.code in {409, 425, 429}
                transient = (exc.code in {408, 409, 425, 429} or 500 <= exc.code < 600) and retry_safe
                if transient and attempt < 5:
                    delay = _retry_after(exc.headers.get("Retry-After"))
                    time.sleep((delay if delay is not None else min(20, .5 * 2**attempt)) + random.uniform(0, .25))
                    continue
                detail = exc.read(2048).decode("utf-8", "replace").replace(self._token, "[REDACTED]")
                exc.close()
                raise NotionError(f"Notion API returned HTTP {exc.code}: {detail}") from None
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt < 5 and not (method == "POST" and path == "/pages"):
                    time.sleep(min(20, .5 * 2**attempt) + random.uniform(0, .25)); continue
                detail = str(exc).replace(self._token, "[REDACTED]")
                raise NotionError(f"Notion API request failed: {detail}") from None
        raise AssertionError("retry loop exhausted")

    def data_source(self, data_source_id: str) -> dict[str, Any]:
        return self.request("GET", f"/data_sources/{urllib.parse.quote(data_source_id, safe='')}")

    def database(self, database_id: str) -> dict[str, Any]:
        return self.request("GET", f"/databases/{urllib.parse.quote(database_id, safe='')}")

    def child_databases(self, page_id: str) -> list[dict[str, Any]]:
        """Return database objects for databases directly contained by a page."""
        databases: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {"page_size": "100"}
            if cursor:
                query["start_cursor"] = cursor
            response = self.request(
                "GET", f"/blocks/{urllib.parse.quote(page_id, safe='')}/children?{urllib.parse.urlencode(query)}"
            )
            for block in response.get("results", []):
                if block.get("type") == "child_database":
                    databases.append(self.database(block["id"]))
            if not response.get("has_more"):
                return databases
            cursor = response.get("next_cursor")
            if not cursor:
                raise NotionError("Notion returned a paginated child list without a next cursor")

    def validate_data_source(self, name: str, data_source_id: str, required: dict[str, str], *, parent_page_id: str | None = None) -> None:
        try:
            source = self.data_source(data_source_id)
        except NotionError as exc:
            message = str(exc)
            if "HTTP 403" in message or "HTTP 404" in message:
                raise SchemaError(
                    f"{name} is not reachable; share that database with the Notion integration "
                    f"and verify its NOTION_DS_* identifier"
                ) from None
            raise SchemaError(f"{name} could not be checked: {message}") from None
        properties = source.get("properties", {})
        errors = []
        for prop, expected in required.items():
            actual = (properties.get(prop) or {}).get("type")
            if actual != expected:
                errors.append(f"{prop!r}: expected {expected}, got {actual or 'missing'}")
        if errors:
            raise SchemaError(f"Notion data source {name!r} ({data_source_id}) is incompatible: " + "; ".join(errors))
        if parent_page_id:
            parent = source.get("parent") or {}
            actual = parent.get("page_id")
            if not actual and parent.get("database_id"):
                database = self.request("GET", f"/databases/{urllib.parse.quote(parent['database_id'], safe='')}")
                database_parent = database.get("parent") or {}
                actual = database_parent.get("page_id")
            if actual != parent_page_id:
                raise SchemaError(f"Notion data source {name!r} is outside approved parent page {parent_page_id!r}")

    def query_data_source(self, data_source_id: str, *, start_cursor: str | None = None, filter: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor: body["start_cursor"] = start_cursor
        if filter: body["filter"] = filter
        return self.request("POST", f"/data_sources/{data_source_id}/query", body)

    def create_page(self, data_source_id: str, properties: dict[str, Any], children: list[dict[str, Any]] | None = None) -> str:
        properties = _safe(properties); children = _safe(children) if children else None
        result = self.request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties, **({"children": children[:100]} if children else {})})
        return result["id"]

    def update_page(self, page_id: str, properties: dict[str, Any], *, archived: bool | None = None) -> None:
        body: dict[str, Any] = {"properties": _safe(properties)}
        if archived is not None: body["archived"] = archived
        self.request("PATCH", f"/pages/{page_id}", body)

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        blocks = _safe(blocks)
        for start in range(0, len(blocks), 100):
            self.request("PATCH", f"/blocks/{page_id}/children", {"children": blocks[start:start + 100]})

    def strip_blocks(self, page_id: str) -> None:
        while True:
            response = self.request("GET", f"/blocks/{page_id}/children?page_size=100")
            for child in response.get("results", []): self.request("DELETE", f"/blocks/{child['id']}")
            if not response.get("has_more") or not response.get("results"): break

    def replace_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        self.strip_blocks(page_id); self.append_blocks(page_id, blocks)

    def find_by_property(self, data_source_id: str, property_name: str, value: str) -> str | None:
        """Recovery-only lookup; normal updates always use the local page index."""
        response = self.request("POST", f"/data_sources/{data_source_id}/query", {"page_size": 2, "filter": {"property": property_name, "rich_text": {"equals": value}}})
        results = response.get("results", [])
        if len(results) > 1: raise NotionError(f"duplicate {property_name}={value!r} in Notion")
        return results[0]["id"] if results else None


def _chunks(value: str, limit: int = 1800) -> list[str]:
    """Split at Python Unicode code-point boundaries (never encoded byte boundaries)."""
    return [value[i:i + limit] for i in range(0, len(value), limit)] or [""]


def _safe(value: Any) -> Any:
    """Final pre-Notion boundary: no caller can accidentally persist unredacted text.

    Walks the structure instead of the serialized blob. Redacting the whole JSON
    string also hits the identity fields that attribute a record to a person --
    "Member Email", "Actor Email", and the members' own "Email" -- which turns
    every row into an unattributable orphan.
    """
    return redact_structure(value, True)[0]


def rich_text(value: str | None) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": part}} for part in _chunks(value or "", 1800)]


def title(value: str) -> dict[str, Any]: return {"title": rich_text(value)}
def text(value: str | None) -> dict[str, Any]: return {"rich_text": rich_text(value)}
def date(value: str | None) -> dict[str, Any]: return {"date": {"start": value} if value else None}
def number(value: int | float) -> dict[str, Any]: return {"number": value}
def select(value: str) -> dict[str, Any]: return {"select": {"name": value}}
def relation(page_id: str | None) -> dict[str, Any]: return {"relation": [{"id": page_id}] if page_id else []}
