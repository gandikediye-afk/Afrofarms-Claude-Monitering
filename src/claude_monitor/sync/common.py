from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..notion_client import NotionClient, date, number, select, text, title
from ..state import State, utcnow


class Run:
    """Persist a local and Notion audit record for an operation."""
    def __init__(self, state: State, notion: NotionClient, sync_runs_ds: str, plane: str):
        self.state, self.notion, self.ds, self.plane = state, notion, sync_runs_ds, plane
        self.id = f"{plane}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}-{uuid.uuid4().hex[:8]}"
        self.start_cursor = state.get_cursor(plane) if plane in {"chats", "activities"} else None
        self.records = self.pages = self.created = self.updated = self.unchanged = self.errors = 0
        self.final_request_id = self.end_cursor = None
        self.started = utcnow()
        state.connection.execute("INSERT INTO run_log VALUES(?,?,?,?,?,?,?,?,?)", (self.id, plane, self.start_cursor, None, 0, None, "running", self.started, None))

    def finish(self, outcome: str = "ok") -> None:
        finished = utcnow()
        self.state.connection.execute("UPDATE run_log SET end_cursor=?,records=?,final_request_id=?,outcome=?,finished_at=? WHERE run_id=?", (self.end_cursor, self.records, self.final_request_id, outcome, finished, self.id))
        props: dict[str, Any] = {
            "Run": title(self.id), "Plane": select(self.plane), "Started": date(self.started), "Finished": date(finished),
            "Start Cursor": text(self.start_cursor), "End Cursor": text(self.end_cursor), "Pages": number(self.pages),
            "Records": number(self.records), "Chats Created": number(self.created), "Chats Updated": number(self.updated),
            "Chats Unchanged": number(self.unchanged), "Errors": number(self.errors),
            "Final Request ID": text(self.final_request_id), "Outcome": select(outcome),
        }
        self.notion.create_page(self.ds, props)
