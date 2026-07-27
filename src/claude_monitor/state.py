"""Transactional SQLite state and durable work queue."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str: return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self.connection.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS cursors (plane TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_index (chat_id TEXT PRIMARY KEY, notion_page_id TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL, message_count INTEGER NOT NULL, deleted_at TEXT, last_synced_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_log (run_id TEXT PRIMARY KEY, plane TEXT NOT NULL, start_cursor TEXT, end_cursor TEXT, records INTEGER NOT NULL DEFAULT 0, final_request_id TEXT, outcome TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS object_index (kind TEXT NOT NULL, object_id TEXT NOT NULL, notion_page_id TEXT NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY(kind, object_id), UNIQUE(kind, notion_page_id));
        CREATE TABLE IF NOT EXISTS work_queue (plane TEXT NOT NULL, object_id TEXT NOT NULL, payload TEXT NOT NULL, enqueued_at TEXT NOT NULL, PRIMARY KEY(plane, object_id));
        CREATE TABLE IF NOT EXISTS claims (kind TEXT NOT NULL, object_id TEXT NOT NULL, claimed_at TEXT NOT NULL, PRIMARY KEY(kind, object_id));
        CREATE TABLE IF NOT EXISTS pending_writes (kind TEXT NOT NULL, object_id TEXT NOT NULL, content_hash TEXT NOT NULL, started_at TEXT NOT NULL, PRIMARY KEY(kind, object_id));
        COMMIT;
        """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback(); raise
        else: self.connection.commit()

    def get_cursor(self, plane: str) -> str | None:
        row = self.connection.execute("SELECT cursor FROM cursors WHERE plane=?", (plane,)).fetchone()
        return row[0] if row else None

    def stage(self, plane: str, object_id: str, payload: dict[str, Any]) -> None:
        # Redacted/normalized payloads only: callers must never stage raw message content.
        self.connection.execute("INSERT INTO work_queue VALUES(?,?,?,?) ON CONFLICT(plane,object_id) DO UPDATE SET payload=excluded.payload,enqueued_at=excluded.enqueued_at", (plane, object_id, json.dumps(payload, sort_keys=True), utcnow()))

    def finish_walk(self, plane: str, cursor: str | None) -> None:
        """Advance only after the caller has durably processed the entire fetched walk."""
        with self.transaction() as db:
            remaining = db.execute("SELECT 1 FROM work_queue WHERE plane=? LIMIT 1", (plane,)).fetchone()
            if remaining: raise RuntimeError(f"cannot advance {plane} cursor with pending durable work")
            db.execute("INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(plane) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at", (plane, cursor, utcnow()))

    def complete_item(self, plane: str, object_id: str) -> None:
        self.connection.execute("DELETE FROM work_queue WHERE plane=? AND object_id=?", (plane, object_id))

    def queued(self, plane: str, limit: int = 100) -> list[sqlite3.Row]:
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
        except sqlite3.IntegrityError: return False

    def release(self, kind: str, object_id: str) -> None:
        self.connection.execute("DELETE FROM claims WHERE kind=? AND object_id=?", (kind, object_id))

    def chat(self, chat_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM chat_index WHERE chat_id=?", (chat_id,)).fetchone()

    def put_chat(self, chat_id: str, page_id: str, content_hash: str, count: int, deleted_at: str | None) -> None:
        self.connection.execute("INSERT INTO chat_index VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET notion_page_id=excluded.notion_page_id,content_hash=excluded.content_hash,message_count=excluded.message_count,deleted_at=excluded.deleted_at,last_synced_at=excluded.last_synced_at", (chat_id, page_id, content_hash, count, deleted_at, utcnow()))

    def object(self, kind: str, object_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM object_index WHERE kind=? AND object_id=?", (kind, object_id)).fetchone()

    def put_object(self, kind: str, object_id: str, page_id: str, content_hash: str, *, db: sqlite3.Connection | None = None) -> None:
        (db or self.connection).execute("INSERT INTO object_index VALUES(?,?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET notion_page_id=excluded.notion_page_id,content_hash=excluded.content_hash", (kind, object_id, page_id, content_hash))

    def begin_write(self, kind: str, object_id: str, content_hash: str) -> None:
        self.connection.execute("INSERT INTO pending_writes VALUES(?,?,?,?) ON CONFLICT(kind,object_id) DO UPDATE SET content_hash=excluded.content_hash,started_at=excluded.started_at", (kind, object_id, content_hash, utcnow()))

    def write_pending(self, kind: str, object_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM pending_writes WHERE kind=? AND object_id=?", (kind, object_id)).fetchone() is not None

    def finish_write(self, kind: str, object_id: str, *, db: sqlite3.Connection | None = None) -> None:
        (db or self.connection).execute("DELETE FROM pending_writes WHERE kind=? AND object_id=?", (kind, object_id))
