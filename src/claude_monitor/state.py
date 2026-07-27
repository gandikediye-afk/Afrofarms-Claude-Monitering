"""Transactional state store and durable work queue.

Backed by SQLite or Postgres; see db.py for the backend selection rules."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from .db import IntegrityViolation, Row, open_backend
from .normalizer import redact_structure


def utcnow() -> str: return datetime.now(timezone.utc).isoformat()


class State:
    """Durable cursors, idempotency indexes, and the work queue.

    Accepts either a filesystem path (SQLite) or a Postgres DSN; the schema and
    every statement below are identical on both backends.
    """

    def __init__(self, target: str | Path):
        self.connection = open_backend(target)

    @property
    def dialect(self) -> str: return self.connection.dialect

    def migrate(self) -> None: self.connection.migrate()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connection.transaction() as db:
            yield db

    def get_cursor(self, plane: str) -> str | None:
        row = self.connection.execute("SELECT cursor FROM cursors WHERE plane=?", (plane,)).fetchone()
        return row[0] if row else None

    def stage(self, plane: str, object_id: str, payload: dict[str, Any]) -> None:
        # Defense in depth: sanitize the payload at the storage boundary too. Walk the
        # structure rather than the serialized blob, so identity fields survive as join
        # keys while every free-text value is still redacted.
        safe_payload, _ = redact_structure(payload, True)
        safe = json.dumps(safe_payload, sort_keys=True)
        self.connection.execute("INSERT INTO work_queue VALUES(?,?,?,?) ON CONFLICT(plane,object_id) DO UPDATE SET payload=excluded.payload,enqueued_at=excluded.enqueued_at", (plane, object_id, safe, utcnow()))

    def record_governance_action(self, page_id: str, source_id: str | None, action: str, reason: str) -> bool:
        cursor = self.connection.execute(
            "INSERT INTO governance_actions(page_id,source_id,action,reason,acted_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (page_id, source_id, action, reason, utcnow()),
        )
        return cursor.rowcount > 0

    def finish_walk(self, plane: str, cursor: str | None) -> None:
        """Advance only after the caller has durably processed the entire fetched walk."""
        with self.transaction() as db:
            remaining = db.execute("SELECT 1 FROM work_queue WHERE plane=? LIMIT 1", (plane,)).fetchone()
            if remaining: raise RuntimeError(f"cannot advance {plane} cursor with pending durable work")
            db.execute("INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(plane) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at", (plane, cursor, utcnow()))

    def complete_item(self, plane: str, object_id: str) -> None:
        self.connection.execute("DELETE FROM work_queue WHERE plane=? AND object_id=?", (plane, object_id))

    def queued(self, plane: str, limit: int = 100) -> list[Row]:
        """Return durable work in FIFO order without removing it."""
        return list(self.connection.execute(
            "SELECT object_id,payload,enqueued_at FROM work_queue WHERE plane=? "
            "ORDER BY enqueued_at LIMIT ?", (plane, limit)
        ))

    def claim(self, kind: str, object_id: str) -> bool:
        """Atomically serialize page creation for a stable upstream identifier."""
        try:
            self.connection.execute("INSERT INTO claims VALUES(?,?,?)", (kind, object_id, utcnow()))
            return True
        except IntegrityViolation: return False

    def release(self, kind: str, object_id: str) -> None:
        self.connection.execute("DELETE FROM claims WHERE kind=? AND object_id=?", (kind, object_id))

    def chat(self, chat_id: str) -> Row | None:
        return self.connection.execute("SELECT * FROM chat_index WHERE chat_id=?", (chat_id,)).fetchone()

    def put_chat(self, chat_id: str, page_id: str, content_hash: str, count: int, deleted_at: str | None) -> None:
        self.connection.execute("INSERT INTO chat_index VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET notion_page_id=excluded.notion_page_id,content_hash=excluded.content_hash,message_count=excluded.message_count,deleted_at=excluded.deleted_at,last_synced_at=excluded.last_synced_at", (chat_id, page_id, content_hash, count, deleted_at, utcnow()))

    def object(self, kind: str, object_id: str) -> Row | None:
        return self.connection.execute("SELECT * FROM object_index WHERE kind=? AND object_id=?", (kind, object_id)).fetchone()

    def put_object(self, kind: str, object_id: str, page_id: str, content_hash: str, *, db: Any | None = None) -> None:
        (db or self.connection).execute("INSERT INTO object_index VALUES(?,?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET notion_page_id=excluded.notion_page_id,content_hash=excluded.content_hash", (kind, object_id, page_id, content_hash))

    def begin_write(self, kind: str, object_id: str, content_hash: str) -> None:
        self.connection.execute("INSERT INTO pending_writes VALUES(?,?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET content_hash=excluded.content_hash,started_at=excluded.started_at", (kind, object_id, content_hash, utcnow()))

    def write_pending(self, kind: str, object_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM pending_writes WHERE kind=? AND object_id=?", (kind, object_id)).fetchone() is not None

    def finish_write(self, kind: str, object_id: str, *, db: Any | None = None) -> None:
        (db or self.connection).execute("DELETE FROM pending_writes WHERE kind=? AND object_id=?", (kind, object_id))
