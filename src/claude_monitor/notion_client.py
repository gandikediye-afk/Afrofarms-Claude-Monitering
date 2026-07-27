"""Rate-limited Notion writer used by all sync planes."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any


class NotionClient:
    def __init__(self, token: str, rps: float = 2.5):
        self._token, self._interval, self._last = token, 1 / rps, 0.0

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(6):
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0: time.sleep(wait)
            request = urllib.request.Request("https://api.notion.com/v1" + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                headers={"Authorization": f"Bearer {self._token}", "Notion-Version": "2025-09-03", "Content-Type": "application/json"})
            try:
                self._last = time.monotonic()
                with urllib.request.urlopen(request, timeout=30) as response: return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in {409, 429} or 500 <= exc.code < 600:
                    if attempt < 5:
                        retry = exc.headers.get("retry-after")
                        delay = float(retry) if retry and retry.replace(".", "", 1).isdigit() else min(20, 0.5 * 2**attempt)
                        time.sleep(delay + random.uniform(0, .25)); continue
                raise RuntimeError(f"Notion API returned HTTP {exc.code}: {exc.read(2048).decode('utf-8', 'replace')}") from exc
        raise AssertionError("retry loop exhausted")

    def create_page(self, data_source_id: str, properties: dict[str, Any], children: list[dict[str, Any]] | None = None) -> str:
        result = self.request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties, **({"children": children} if children else {})})
        return result["id"]

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None: self.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        for start in range(0, len(blocks), 100): self.request("PATCH", f"/blocks/{page_id}/children", {"children": blocks[start:start + 100]})

    def replace_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        cursor = None
        while True:
            suffix = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            response = self.request("GET", f"/blocks/{page_id}/children{suffix}")
            for child in response.get("results", []): self.request("DELETE", f"/blocks/{child['id']}")
            if not response.get("has_more"): break
            cursor = response.get("next_cursor")
        self.append_blocks(page_id, blocks)


def title(value: str) -> dict[str, Any]: return {"title": [{"text": {"content": value[:2000]}}]}
def text(value: str | None) -> dict[str, Any]: return {"rich_text": [{"text": {"content": (value or "")[:2000]}}]}
def date(value: str | None) -> dict[str, Any]: return {"date": {"start": value} if value else None}
def number(value: int | float) -> dict[str, Any]: return {"number": value}
def select(value: str) -> dict[str, Any]: return {"select": {"name": value}}
def relation(page_id: str | None) -> dict[str, Any]: return {"relation": [{"id": page_id}] if page_id else []}
