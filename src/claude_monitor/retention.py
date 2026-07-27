"""Scheduled deletion and retention enforcement for mirrored conversations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .notion_client import NotionClient, date, number, relation, text, title
from .state import State


def _value(prop: dict[str, Any] | None) -> str | None:
    prop = prop or {}
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind == "date": return (value or {}).get("start")
    if kind == "select": return (value or {}).get("name")
    if kind in {"title", "rich_text"}: return "".join(x.get("plain_text", "") for x in value or [])
    return None


def _instant(value: str | None) -> datetime | None:
    if not value: return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def tombstone_properties(reason: str, when: datetime) -> dict[str, Any]:
    """Keep identifiers/governance dates but remove identifying and content-derived metadata."""
    return {
        "Title": title("(tombstoned)"), "Member": relation(None), "Member Email": {"email": None},
        "Project": relation(None), "Open in Claude": {"url": None}, "Messages": number(0),
        "Member Turns": number(0), "Claude Turns": number(0), "Transcript Chars": number(0),
        "Attachments": number(0), "Generated Files": number(0), "Artifacts": number(0),
        "Content Hash": text(None), "Flags": {"multi_select": [{"name": reason}]},
        "Tombstoned": {"checkbox": True}, "Last Synced": date(when.isoformat()),
    }


class RetentionWorker:
    def __init__(self, notion: NotionClient, state: State, config: Config):
        self.notion, self.state, self.config = notion, state, config

    def _remove_message_rows(self, conversation_page_id: str) -> None:
        if not self.config.notion_ds_messages: return
        cursor = None
        while True:
            response = self.notion.query_data_source(
                self.config.notion_ds_messages, start_cursor=cursor,
                filter={"property": "Conversation", "relation": {"contains": conversation_page_id}},
            )
            for page in response.get("results", []):
                self.notion.strip_blocks(page["id"])
                self.notion.update_page(page["id"], {}, archived=True)
            if not response.get("has_more"): break
            cursor = response.get("next_cursor")

    def _tombstone(self, page_id: str, chat_id: str | None, reason: str, now: datetime) -> None:
        self.notion.strip_blocks(page_id)
        self._remove_message_rows(page_id)
        self.notion.update_page(page_id, tombstone_properties(reason, now), archived=True)
        self.state.record_governance_action(page_id, chat_id, "tombstone-and-archive", reason)

    def run_once(self, *, now: datetime | None = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        actions = 0
        cursor = None
        while True:
            response = self.notion.query_data_source(self.config.notion_ds_conversations, start_cursor=cursor)
            for page in response.get("results", []):
                props = page.get("properties") or {}
                if page.get("archived") or (props.get("Tombstoned") or {}).get("checkbox"):
                    continue
                retention_class = _value(props.get("Retention Class")) or "standard"
                if retention_class == "legal-hold":
                    continue
                if retention_class not in self.config.retention_class_days:
                    raise RuntimeError(f"unconfigured retention class {retention_class!r} on page {page['id']}")
                page_id, chat_id = page["id"], _value(props.get("Chat ID"))
                deleted_at = _instant(_value(props.get("Deleted At")))
                last_activity = _instant(_value(props.get("Last Activity")))
                reason = None
                if deleted_at and now >= deleted_at + timedelta(seconds=self.config.deletion_grace_period):
                    reason = "soft-delete-grace-expired"
                elif last_activity:
                    if now >= last_activity + timedelta(days=self.config.retention_class_days[retention_class]):
                        reason = "retention-expired"
                if reason:
                    self._tombstone(page_id, chat_id, reason, now); actions += 1
            if not response.get("has_more"): break
            cursor = response.get("next_cursor")
            if not cursor: raise RuntimeError("Notion retention query omitted next_cursor")
        return actions
