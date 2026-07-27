"""Mock Compliance API and Notion API for driving the real pipeline offline.

Both clients expose an injection seam: AnthropicClient builds `_opener`, and
NotionClient accepts `opener=`. Swapping those runs every layer of the real
pipeline -- pagination, retries, normalization, redaction, state, writer --
against fixtures, without a network or a live token.
"""

from __future__ import annotations

import io
import json
import urllib.parse
from typing import Any


class _Response(io.BytesIO):
    """Enough of http.client.HTTPResponse for both clients."""

    def __init__(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None):
        body = json.dumps(payload).encode()
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = {"request-id": "req_mock_0001", **(headers or {})}

    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False
    def getcode(self): return self.status
    def info(self): return self.headers


class MockCompliance:
    """Serves /v1/compliance/* from fixtures, honouring cursor pagination."""

    def __init__(self, fixtures: dict[str, Any]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def open(self, request, timeout=None):  # noqa: ARG002 - urlopen signature
        url = request.full_url if hasattr(request, "full_url") else str(request)
        parts = urllib.parse.urlsplit(url)
        path = parts.path.replace("/v1/compliance", "", 1)
        query = urllib.parse.parse_qs(parts.query)
        self.calls.append((path, query))

        if path == "/apps/chats":
            # The real endpoint sorts ascending by order_by, so an edited chat gets a
            # fresh updated_at and re-appears *after* a saved cursor. Reproduce that,
            # otherwise incremental sync looks broken when it is not.
            order_by = query.get("order_by", ["created_at"])[0]
            chats = sorted(self.fixtures["chats"], key=lambda c: (str(c.get(order_by) or ""), c["id"]))
            return _Response(self._page(chats, query, "chat"))
        if path.startswith("/apps/chats/") and path.endswith("/messages"):
            chat_id = urllib.parse.unquote(path.split("/")[3])
            payload = dict(self.fixtures["messages"][chat_id])
            payload.setdefault("has_more", False)
            return _Response(payload)
        if path == "/activities":
            return _Response(self._page(self.fixtures.get("activities", []), query, "activity"))
        if path == "/users":
            return _Response(self._page(self.fixtures.get("users", []), query, "user"))
        if path == "/apps/projects":
            return _Response(self._page(self.fixtures.get("projects", []), query, "project"))
        raise AssertionError(f"unmocked Compliance path: {path}")

    @staticmethod
    def _page(items: list[dict[str, Any]], query: dict[str, list[str]], _kind: str) -> dict[str, Any]:
        limit = int(query.get("limit", ["100"])[0])
        after = query.get("after_id", [None])[0]
        start = 0
        if after:
            ids = [item["id"] for item in items]
            start = ids.index(after) + 1 if after in ids else len(items)
        window = items[start:start + limit]
        has_more = start + limit < len(items)
        return {"data": window, "has_more": has_more,
                "first_id": window[0]["id"] if window else None,
                "last_id": window[-1]["id"] if window else None}


class MockNotion:
    """Records every write and returns realistic page identifiers."""

    def __init__(self):
        self.pages: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.query_results: list[dict[str, Any]] = []
        self._n = 0

    def open(self, request, timeout=None):  # noqa: ARG002
        method = request.get_method()
        path = urllib.parse.urlsplit(request.full_url).path.replace("/v1", "", 1)
        body = json.loads(request.data.decode()) if request.data else {}
        self.calls.append((method, path))

        if method == "POST" and path == "/pages":
            self._n += 1
            page_id = f"page_{self._n:04d}"
            self.pages[page_id] = {"id": page_id,
                                   "parent": body.get("parent", {}),
                                   "properties": body.get("properties", {})}
            self.blocks[page_id] = self._with_ids(page_id, body.get("children") or [])
            return _Response({"id": page_id, "object": "page"})

        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            page_id = path.split("/")[2]
            existing = self.blocks.setdefault(page_id, [])
            existing.extend(self._with_ids(page_id, body.get("children") or []))
            return _Response({"object": "list", "results": []})

        if method == "DELETE" and path.startswith("/blocks/"):
            block_id = path.split("/")[2]
            for page_id, blocks in self.blocks.items():
                self.blocks[page_id] = [b for b in blocks if b.get("id") != block_id]
            return _Response({"object": "block", "id": block_id, "archived": True})

        if method == "PATCH" and path.startswith("/pages/"):
            page_id = path.split("/")[2]
            page = self.pages.setdefault(page_id, {"id": page_id, "properties": {}})
            if "properties" in body:
                page["properties"].update(body["properties"])
            if body.get("archived"):
                page["archived"] = True
            return _Response({"id": page_id, "object": "page"})

        if method == "GET" and path.startswith("/blocks/"):
            page_id = path.split("/")[2]
            return _Response({"results": self.blocks.get(page_id, []), "has_more": False,
                              "next_cursor": None})

        if method == "POST" and path.endswith("/query"):
            return _Response({"results": self.query_results, "has_more": False, "next_cursor": None})

        if method == "GET" and (path.startswith("/databases/") or path.startswith("/data_sources/")):
            return _Response({"id": path.split("/")[2], "parent": {"page_id": "parent-page"},
                              "title": [], "properties": {}})

        raise AssertionError(f"unmocked Notion call: {method} {path}")

    def _with_ids(self, page_id: str, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Real Notion returns block objects carrying ids; strip_blocks needs them."""
        stamped = []
        for child in children:
            self._n += 1
            stamped.append({**child, "id": f"block_{page_id}_{self._n:05d}"})
        return stamped

    # -- helpers for assertions -------------------------------------------------
    def prop(self, page_id: str, name: str) -> Any:
        return self.pages[page_id]["properties"].get(name)

    def transcript(self, page_id: str) -> str:
        out = []
        for block in self.blocks.get(page_id, []):
            kind = block.get("type")
            for span in block.get(kind, {}).get("rich_text", []):
                out.append(span.get("text", {}).get("content", ""))
        return "\n".join(out)

    def pages_in(self, data_source_id: str) -> list[str]:
        return [pid for pid, page in self.pages.items()
                if page.get("parent", {}).get("data_source_id") == data_source_id]
