"""High-level, indexed and crash-recoverable Notion writes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import Config
from .notion_client import NotionClient, date, number, relation, select, text, title
from .state import State


SCHEMAS: dict[str, dict[str, str]] = {
    "members": {"Name": "title", "User ID": "rich_text", "Email": "email", "Notion Person": "people", "Organization": "rich_text", "Claude Role": "select", "Groups": "multi_select", "Department": "select", "First Seen": "date", "Last Active": "date", "Account Status": "select", "Synced At": "date"},
    "projects": {"Project Name": "title", "Project ID": "rich_text", "Owner": "relation", "Created": "date", "Attachments": "number", "Docs": "number", "Retention": "select", "Synced At": "date"},
    "sync_runs": {"Run": "title", "Plane": "select", "Started": "date", "Finished": "date", "Start Cursor": "rich_text", "End Cursor": "rich_text", "Pages": "number", "Records": "number", "Chats Created": "number", "Chats Updated": "number", "Chats Unchanged": "number", "Errors": "number", "Final Request ID": "rich_text", "Outcome": "select"},
    "conversations": {"Title": "title", "Chat ID": "rich_text", "Member": "relation", "Member Email": "email", "Surface": "select", "Model": "select", "Project": "relation", "Started": "date", "Last Activity": "date", "Messages": "number", "Member Turns": "number", "Claude Turns": "number", "Transcript Chars": "number", "Attachments": "number", "Generated Files": "number", "Artifacts": "number", "Open in Claude": "url", "Deleted At": "date", "Tombstoned": "checkbox", "Retention Class": "select", "Flags": "multi_select", "Review Status": "status", "Content Hash": "rich_text", "Last Synced": "date", "Sync Run": "relation"},
    "activity": {"Event": "title", "Event ID": "rich_text", "Plane": "select", "Event Type": "rich_text", "Actor": "relation", "Actor Email": "email", "Actor Type": "select", "Surface": "select", "Occurred At": "date", "Prompt ID": "rich_text", "Conversation": "relation", "IP Address": "rich_text", "User Agent": "rich_text", "Cells Read": "number", "Cells Written": "number", "Cells Copied": "number", "Attributes": "rich_text", "Ingested At": "date"},
    "messages": {"Excerpt": "title", "Message ID": "rich_text", "Conversation": "relation", "Role": "select", "Sent At": "date", "Member Email": "email", "Characters": "number", "Attachments": "number", "Generated Files": "number", "Artifacts": "number", "Flags": "multi_select"},
}

DISPLAY_TO_SCHEMA = {
    "Members": "members", "Projects": "projects", "Sync Runs": "sync_runs",
    "Conversations": "conversations", "Agent Activity": "activity", "Messages": "messages",
}


def check_notion_config(notion: NotionClient, sources: dict[str, str]) -> list[tuple[str, str | None]]:
    """Check every configured source, returning one result per source."""
    results: list[tuple[str, str | None]] = []
    for label, source_id in sources.items():
        try:
            notion.validate_data_source(label, source_id, SCHEMAS[DISPLAY_TO_SCHEMA[label]])
        except Exception as exc:
            results.append((label, str(exc)))
        else:
            results.append((label, None))
    return results


class Writer:
    def __init__(self, notion: NotionClient, state: State, config: Config):
        self.notion, self.state, self.config = notion, state, config

    def validate_schema(self) -> None:
        sources = {"members": self.config.notion_ds_members, "projects": self.config.notion_ds_projects,
                   "sync_runs": self.config.notion_ds_sync_runs, "conversations": self.config.notion_ds_conversations,
                   "activity": self.config.notion_ds_activity}
        if self.config.notion_ds_messages: sources["messages"] = self.config.notion_ds_messages
        for name, source in sources.items():
            self.notion.validate_data_source(name, source, SCHEMAS[name], parent_page_id=self.config.notion_parent_page_id)

    def upsert(self, kind: str, source_id: str, data_source_id: str, id_property: str,
               properties: dict[str, Any], *, content_hash: str | None = None) -> tuple[str, bool]:
        """Upsert using the local index; query Notion only to recover an interrupted create."""
        digest = content_hash or hashlib.sha256(json.dumps(properties, sort_keys=True).encode()).hexdigest()
        row = self.state.object(kind, source_id)
        if row and row["content_hash"] == digest: return row["notion_page_id"], False
        if row:
            self.notion.update_page(row["notion_page_id"], properties); page_id = row["notion_page_id"]
        else:
            recovering = self.state.write_pending(kind, source_id)
            self.state.begin_write(kind, source_id, digest)
            page_id = self.notion.find_by_property(data_source_id, id_property, source_id) if recovering else None
            if page_id: self.notion.update_page(page_id, properties)
            else: page_id = self.notion.create_page(data_source_id, properties)
        with self.state.transaction() as db:
            self.state.put_object(kind, source_id, page_id, digest, db=db)
            self.state.finish_write(kind, source_id, db=db)
        return page_id, True

    def write_messages(self, messages: list[dict[str, Any]], conversation_page_id: str, member_email: str | None = None) -> None:
        if not self.config.notion_ds_messages: return
        for message in messages:
            message_id = str(message["id"])
            body = "".join(str(p.get("text", "")) for p in message.get("content", []))
            props = {"Excerpt": title(body[:80] or "(empty)"), "Message ID": text(message_id),
                     "Conversation": relation(conversation_page_id), "Role": select(str(message.get("role", "unknown"))),
                     "Sent At": date(message.get("created_at")), "Member Email": {"email": member_email},
                     "Characters": number(len(body)), "Attachments": number(len(message.get("files", []))),
                     "Generated Files": number(len(message.get("generated_files", []))), "Artifacts": number(len(message.get("artifacts", [])))}
            self.upsert("message", message_id, self.config.notion_ds_messages, "Message ID", props)

    def enforce_deletion(self, chat_id: str, *, hard_deleted: bool = False, soft_deleted: bool = False,
                         legal_hold: bool = False) -> None:
        row = self.state.chat(chat_id)
        if not row or legal_hold: return
        page_id = row["notion_page_id"]
        if hard_deleted:
            self.notion.strip_blocks(page_id)
            self.notion.update_page(page_id, {"Tombstoned": {"checkbox": True}})
        elif soft_deleted:
            self.notion.update_page(page_id, {"Deleted At": date(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())})
