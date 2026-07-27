"""Incremental and initial chat synchronization."""

from __future__ import annotations

from typing import Any

from ..anthropic_client import AnthropicClient, ComplianceError
from ..config import Config
from ..normalizer import canonical_chat, normalize_messages, redact, transcript_blocks
from ..notion_client import NotionClient, date, number, relation, select, text, title
from ..state import State
from ..writer import Writer
from ..retention import tombstone_properties
from .common import Run


def _all_messages(client: AnthropicClient, chat_id: str, threshold: int) -> list[dict[str, Any]]:
    first = client.messages(chat_id)
    result = list(first.data)
    # APIs only paginate large responses; follow opaque cursors whenever advertised.
    cursor = first.last_id
    while first.has_more:
        first = client.messages(chat_id, limit=threshold, after_id=cursor)
        result.extend(first.data); cursor = first.last_id
    return result


def _properties(chat: dict[str, Any], normalized: dict[str, Any], digest: str, state: State) -> dict[str, Any]:
    user = chat.get("user") or {}; user_row = state.object("user", str(user.get("id"))) if user.get("id") else None
    project_row = state.object("project", str(chat.get("project_id"))) if chat.get("project_id") else None
    chars = sum(len(part["text"]) for msg in normalized["messages"] for part in msg["content"])
    return {"Title": title(chat.get("name") or "(untitled)"), "Chat ID": text(chat.get("id")),
        "Member": relation(user_row["notion_page_id"] if user_row else None), "Member Email": {"email": user.get("email_address") if "@" in str(user.get("email_address") or "") else None},
        "Surface": select("claude.ai"), "Model": select(chat.get("model") if chat.get("model") in {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"} else "other"),
        "Project": relation(project_row["notion_page_id"] if project_row else None), "Started": date(chat.get("created_at")),
        "Last Activity": date(chat.get("updated_at")), "Messages": number(len(normalized["messages"])),
        "Member Turns": number(normalized["roles"].get("user", 0)), "Claude Turns": number(normalized["roles"].get("assistant", 0)),
        "Transcript Chars": number(chars), "Attachments": number(normalized["attachments"]),
        "Generated Files": number(normalized["generated_files"]), "Artifacts": number(normalized["artifacts"]),
        "Open in Claude": {"url": chat.get("href")}, "Deleted At": date(chat.get("deleted_at")), "Tombstoned": {"checkbox": False},
        "Flags": {"multi_select": [{"name": flag} for flag in normalized["flags"]]}, "Content Hash": text(digest), "Last Synced": date(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())}


def _process(chat: dict[str, Any], client: AnthropicClient, notion: NotionClient, state: State, config: Config, *, dry_run: bool) -> str:
    chat_id = str(chat["id"])
    try: messages = _all_messages(client, chat_id, config.message_paging_threshold)
    except ComplianceError as exc:
        if exc.status != 404: raise
        row = state.chat(chat_id)
        if row and not dry_run:
            from datetime import datetime, timezone
            when = datetime.now(timezone.utc)
            notion.strip_blocks(row["notion_page_id"])
            props = tombstone_properties("upstream-hard-delete-or-expiry", when)
            props["Deleted At"] = date(chat.get("deleted_at") or chat.get("updated_at") or when.isoformat())
            notion.update_page(row["notion_page_id"], props)
            state.record_governance_action(row["notion_page_id"], chat_id, "tombstone", "upstream-hard-delete-or-expiry")
            state.put_chat(chat_id, row["notion_page_id"], row["content_hash"], row["message_count"], chat.get("deleted_at") or chat.get("updated_at"))
        return "tombstoned"
    normalized = normalize_messages(messages, config.redaction_enabled)
    safe_chat = dict(chat)
    safe_chat["name"], name_flags = redact(str(chat.get("name") or ""), config.redaction_enabled)
    safe_user = dict(chat.get("user") or {})
    if safe_user.get("email_address"):
        safe_user["email_address"], email_flags = redact(str(safe_user["email_address"]), config.redaction_enabled)
        name_flags |= email_flags
    safe_chat["user"] = safe_user
    normalized["flags"] = sorted(set(normalized["flags"]) | name_flags)
    canonical, digest = canonical_chat(safe_chat, normalized)
    # Only sanitized content is allowed into durable storage.
    state.stage("chats", chat_id, {"chat": {k: safe_chat.get(k) for k in ("id", "name", "created_at", "updated_at", "deleted_at")}, "canonical": canonical})
    # The stable Claude chat ID is the only lookup key.  The durable local index avoids a
    # Notion query on every poll and makes email/name changes irrelevant to identity.
    row = state.chat(chat_id)
    if row and row["content_hash"] == digest:
        state.complete_item("chats", chat_id); return "unchanged"
    if dry_run:
        state.complete_item("chats", chat_id); return "updated" if row else "created"
    if not state.claim("chat", chat_id): raise RuntimeError(f"chat {chat_id} is already being processed")
    try:
        props = _properties(safe_chat, normalized, digest, state)
        if row:
            notion.update_page(row["notion_page_id"], props); page_id = row["notion_page_id"]
            if config.mirror_transcripts: notion.replace_blocks(page_id, transcript_blocks(normalized))
            outcome = "updated"
        else:
            blocks = transcript_blocks(normalized) if config.mirror_transcripts else []
            page_id = notion.create_page(config.notion_ds_conversations, props, blocks[:100] or None)
            if len(blocks) > 100: notion.append_blocks(page_id, blocks[100:])
            outcome = "created"
        state.put_chat(chat_id, page_id, digest, len(messages), chat.get("deleted_at")); state.complete_item("chats", chat_id)
        Writer(notion, state, config).write_messages(normalized["messages"], page_id, safe_user.get("email_address"))
        if chat.get("deleted_at"):
            # Mark now; the retention worker removes content after the configured grace period.
            notion.update_page(page_id, {"Deleted At": date(chat["deleted_at"])})
        return outcome
    finally: state.release("chat", chat_id)


def sync(client: AnthropicClient, notion: NotionClient, state: State, config: Config, *, backfill: bool = False, dry_run: bool = False) -> Run:
    plane = "backfill" if backfill else "chats"; run = Run(state, notion, config.notion_ds_sync_runs, plane)
    cursor = None if backfill else state.get_cursor("chats")
    params: dict[str, Any] = {"order_by": "created_at" if backfill else "updated_at", "limit": config.chat_page_limit}
    if cursor: params["after_id"] = cursor
    try:
        while True:
            page = client.chats(**params); run.pages += 1; run.final_request_id = page.request_id
            for chat in page.data:
                outcome = _process(chat, client, notion, state, config, dry_run=dry_run); run.records += 1
                if outcome == "created": run.created += 1
                elif outcome == "updated": run.updated += 1
                elif outcome == "unchanged": run.unchanged += 1
            if page.last_id: params["after_id"] = page.last_id; run.end_cursor = page.last_id
            if not page.has_more: break
        if not backfill and not dry_run: state.finish_walk("chats", run.end_cursor)
        run.finish(); return run
    except Exception:
        run.errors += 1; run.finish("failed"); raise
