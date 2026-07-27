"""Allowlisted Compliance Activity Feed synchronization."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..anthropic_client import AnthropicClient
from ..config import Config
from ..normalizer import redact
from ..notion_client import NotionClient, date, relation, select, text, title
from ..state import State, utcnow
from .common import Run


def sync(client: AnthropicClient, notion: NotionClient, state: State, config: Config) -> Run:
    run = Run(state, notion, config.notion_ds_sync_runs, "activities")
    cursor = state.get_cursor("activities"); params: dict[str, Any] = {"limit": config.activity_page_limit}
    if config.activity_type_allowlist: params["activity_types[]"] = list(config.activity_type_allowlist)
    if cursor: params["before_id"] = cursor
    newest = cursor
    try:
        while True:
            page = client.activities(**params); run.pages += 1; run.final_request_id = page.request_id
            if page.first_id: newest = page.first_id if newest == cursor else newest
            for event in page.data:
                event_id = str(event["id"]); safe_json, _ = redact(json.dumps(event, sort_keys=True, separators=(",", ":")), config.redaction_enabled)
                state.stage("activities", event_id, {"event": json.loads(safe_json)})
                safe_event = json.loads(safe_json)
                digest = hashlib.sha256(safe_json.encode()).hexdigest(); row = state.object("activity", event_id)
                if not row or row["content_hash"] != digest:
                    actor = safe_event.get("actor") or {}; actor_row = state.object("user", str(actor.get("id"))) if actor.get("id") else None
                    chat_id = safe_event.get("chat_id") or (safe_event.get("data") or {}).get("chat_id"); chat_row = state.chat(str(chat_id)) if chat_id else None
                    props = {"Event": title(f"{safe_event.get('type', 'unknown')} · {actor.get('email_address', '')}"), "Event ID": text(event_id),
                        "Plane": select("compliance-activity"), "Event Type": text(safe_event.get("type")), "Actor": relation(actor_row["notion_page_id"] if actor_row else None),
                        "Actor Email": {"email": actor.get("email_address") if "@" in str(actor.get("email_address") or "") else None}, "Actor Type": select(actor.get("type") or "unknown"),
                        "Surface": select("claude.ai"), "Occurred At": date(safe_event.get("created_at")), "Conversation": relation(chat_row["notion_page_id"] if chat_row else None),
                        "IP Address": text(safe_event.get("ip_address")), "User Agent": text(safe_event.get("user_agent")), "Attributes": text(safe_json), "Ingested At": date(utcnow())}
                    if row: notion.update_page(row["notion_page_id"], props); page_id = row["notion_page_id"]
                    else: page_id = notion.create_page(config.notion_ds_activity, props)
                    state.put_object("activity", event_id, page_id, digest)
                state.complete_item("activities", event_id); run.records += 1
            if not page.has_more: break
            if not page.first_id: raise RuntimeError("activity page omitted cursor")
            params["before_id"] = page.first_id
        run.end_cursor = newest; state.finish_walk("activities", newest); run.finish(); return run
    except Exception:
        run.errors += 1; run.finish("failed"); raise
