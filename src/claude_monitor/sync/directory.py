"""User and project directory synchronization."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..anthropic_client import AnthropicClient
from ..config import Config
from ..notion_client import NotionClient, date, number, relation, select, text, title
from ..state import State, utcnow
from .common import Run


def _digest(item: dict[str, Any]) -> str: return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _upsert(kind: str, item: dict[str, Any], ds: str, props: dict[str, Any], notion: NotionClient, state: State) -> str:
    object_id, digest = str(item["id"]), _digest(item); row = state.object(kind, object_id)
    if row and row["content_hash"] == digest: return "unchanged"
    if row: notion.update_page(row["notion_page_id"], props); page_id = row["notion_page_id"]
    else: page_id = notion.create_page(ds, props)
    state.put_object(kind, object_id, page_id, digest); return "updated" if row else "created"


def sync(client: AnthropicClient, notion: NotionClient, state: State, config: Config) -> Run:
    run = Run(state, notion, config.notion_ds_sync_runs, "directory")
    try:
        for page in client.pages("/users", {"limit": 100}):
            run.pages += 1; run.final_request_id = page.request_id
            for user in page.data:
                props = {"Name": title(user.get("name") or user.get("email_address") or "(unknown)"), "User ID": text(user.get("id")),
                    "Email": {"email": user.get("email_address")}, "Organization": text((user.get("organization") or {}).get("name") or user.get("organization_id")),
                    "Claude Role": select(user.get("role") or "unknown"), "First Seen": date(user.get("created_at")),
                    "Last Active": date(user.get("last_active_at")), "Account Status": select(user.get("status") or "unknown"), "Synced At": date(utcnow())}
                _upsert("user", user, config.notion_ds_members, props, notion, state); run.records += 1
        for page in client.pages("/apps/projects", {"limit": 100}):
            run.pages += 1; run.final_request_id = page.request_id
            for project in page.data:
                owner = state.object("user", str(project.get("owner_id"))) if project.get("owner_id") else None
                props = {"Project Name": title(project.get("name") or "(untitled)"), "Project ID": text(project.get("id")),
                    "Owner": relation(owner["notion_page_id"] if owner else None), "Created": date(project.get("created_at")),
                    "Attachments": number(len(project.get("attachments") or [])), "Docs": number(len(project.get("documents") or [])), "Synced At": date(utcnow())}
                _upsert("project", project, config.notion_ds_projects, props, notion, state); run.records += 1
        run.finish(); return run
    except Exception:
        run.errors += 1; run.finish("failed"); raise
