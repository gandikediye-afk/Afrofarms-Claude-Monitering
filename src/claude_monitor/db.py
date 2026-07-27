"""Storage backends for the state store.

SQLite is the default and suits any host with a persistent disk. Serverless
platforms (Vercel, Lambda) have no durable filesystem, and losing the state file
loses the chat_id -> notion_page_id index, which makes the writer create a second
Notion page for every chat it has already mirrored. Point `DATABASE_URL` at
Postgres there instead.

Both backends speak one dialect: `?` placeholders, `ON CONFLICT ... DO UPDATE
SET x=excluded.x`, and rows addressable by column name. Driver differences are
normalized here so `state.py` stays dialect-free.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

POSTGRES_SCHEMES = {"postgres", "postgresql", "postgresql+pg8000"}


class IntegrityViolation(Exception):
    """A uniqueness constraint rejected the write, on either backend."""


def is_postgres_dsn(value: str) -> bool:
    return urllib.parse.urlsplit(str(value)).scheme in POSTGRES_SCHEMES


# One logical schema; the identity column is the only dialect difference.
_TABLES = (
    ("cursors", "plane TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT NOT NULL"),
    ("chat_index",
     "chat_id TEXT PRIMARY KEY, notion_page_id TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL, "
     "message_count INTEGER NOT NULL, deleted_at TEXT, last_synced_at TEXT NOT NULL"),
    ("run_log",
     "run_id TEXT PRIMARY KEY, plane TEXT NOT NULL, start_cursor TEXT, end_cursor TEXT, "
     "records INTEGER NOT NULL DEFAULT 0, final_request_id TEXT, outcome TEXT NOT NULL, "
     "started_at TEXT NOT NULL, finished_at TEXT"),
    ("object_index",
     "kind TEXT NOT NULL, object_id TEXT NOT NULL, notion_page_id TEXT NOT NULL, "
     "content_hash TEXT NOT NULL, PRIMARY KEY(kind, object_id), UNIQUE(kind, notion_page_id)"),
    ("work_queue",
     "plane TEXT NOT NULL, object_id TEXT NOT NULL, payload TEXT NOT NULL, "
     "enqueued_at TEXT NOT NULL, PRIMARY KEY(plane, object_id)"),
    ("claims", "kind TEXT NOT NULL, object_id TEXT NOT NULL, claimed_at TEXT NOT NULL, "
               "PRIMARY KEY(kind, object_id)"),
    ("pending_writes",
     "kind TEXT NOT NULL, object_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
     "started_at TEXT NOT NULL, PRIMARY KEY(kind, object_id)"),
    ("governance_actions",
     "id {autoinc}, page_id TEXT NOT NULL, source_id TEXT, action TEXT NOT NULL, "
     "reason TEXT NOT NULL, acted_at TEXT NOT NULL, UNIQUE(page_id, action, reason)"),
)


def _ddl(autoinc: str) -> list[str]:
    return [f"CREATE TABLE IF NOT EXISTS {name} ({body.format(autoinc=autoinc)})"
            for name, body in _TABLES]


class Row:
    """Column-addressable row, matching sqlite3.Row's access patterns."""

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        self._columns, self._values = columns, tuple(values)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._columns.index(key)]

    def keys(self) -> list[str]: return list(self._columns)
    def __iter__(self) -> Iterator[Any]: return iter(self._values)
    def __len__(self) -> int: return len(self._values)
    def __repr__(self) -> str: return f"Row({dict(zip(self._columns, self._values))})"


class _Result:
    """Cursor-shaped result so callers can chain .fetchone() or iterate."""

    __slots__ = ("rows", "rowcount")

    def __init__(self, rows: list[Any], rowcount: int):
        self.rows, self.rowcount = rows, rowcount

    def fetchone(self) -> Any | None: return self.rows[0] if self.rows else None
    def fetchall(self) -> list[Any]: return self.rows
    def __iter__(self) -> Iterator[Any]: return iter(self.rows)


class SqliteBackend:
    """Single connection, autocommit, WAL. Requires a persistent filesystem."""

    dialect = "sqlite"

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")

    def migrate(self) -> None:
        with self.transaction():
            for statement in _ddl("INTEGER PRIMARY KEY AUTOINCREMENT"):
                self._connection.execute(statement)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        try:
            cursor = self._connection.execute(sql, tuple(params))
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(str(exc)) from exc
        rows = cursor.fetchall() if cursor.description else []
        return _Result(rows, cursor.rowcount)

    @contextmanager
    def transaction(self) -> Iterator["SqliteBackend"]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self._connection.rollback(); raise
        else:
            self._connection.commit()

    def close(self) -> None: self._connection.close()


class PostgresBackend:
    """One connection per instantiation; safe for a serverless invocation.

    Use a pooled DSN (PgBouncer / Neon pooler) so cold starts do not exhaust
    backend connections.
    """

    dialect = "postgres"

    def __init__(self, dsn: str):
        try:
            import pg8000.dbapi as driver
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "DATABASE_URL is set to Postgres but pg8000 is not installed; "
                "install claude-monitor with the 'postgres' extra"
            ) from exc
        driver.paramstyle = "qmark"
        self._driver = driver
        self._connection = driver.connect(**_dsn_kwargs(dsn))
        self._connection.autocommit = True

    def migrate(self) -> None:
        with self.transaction():
            for statement in _ddl("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"):
                self.execute(statement)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        cursor = self._connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
        except Exception as exc:
            # pg8000 surfaces a unique violation as DatabaseError carrying
            # SQLSTATE 23505, not as DB-API IntegrityError, so catching
            # IntegrityError here would let claim() crash instead of reporting
            # that another worker already holds the row.
            if _sqlstate(exc) == "23505":
                raise IntegrityViolation(str(exc)) from exc
            raise
        rows: list[Any] = []
        if cursor.description:
            columns = [d[0] for d in cursor.description]
            rows = [Row(columns, values) for values in cursor.fetchall()]
        return _Result(rows, cursor.rowcount)

    @contextmanager
    def transaction(self) -> Iterator["PostgresBackend"]:
        self._connection.autocommit = False
        try:
            yield self
        except BaseException:
            self._connection.rollback(); raise
        else:
            self._connection.commit()
        finally:
            self._connection.autocommit = True

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:  # pragma: no cover - already-closed sockets
            pass


def _sqlstate(exc: Exception) -> str | None:
    """Extract SQLSTATE from a pg8000 error, whose args carry the server fields."""
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            code = arg.get("C")
            if code:
                return str(code)
    return None


def _dsn_kwargs(dsn: str) -> dict[str, Any]:
    """Translate a libpq URL into pg8000 connect() arguments."""
    parts = urllib.parse.urlsplit(dsn)
    if parts.scheme not in POSTGRES_SCHEMES:
        raise ValueError(f"not a Postgres DSN: {parts.scheme!r}")
    query = dict(urllib.parse.parse_qsl(parts.query))
    kwargs: dict[str, Any] = {
        "user": urllib.parse.unquote(parts.username or "postgres"),
        "host": parts.hostname or "localhost",
        "port": parts.port or 5432,
        "database": urllib.parse.unquote((parts.path or "/postgres").lstrip("/")) or "postgres",
    }
    if parts.password:
        kwargs["password"] = urllib.parse.unquote(parts.password)
    sslmode = query.get("sslmode", "require" if kwargs["host"] not in {"localhost", "127.0.0.1"} else "disable")
    if sslmode not in {"disable", "allow"}:
        import ssl
        context = ssl.create_default_context()
        if sslmode in {"require", "prefer"}:
            # Managed providers terminate TLS on a pooler whose certificate does
            # not always match the DSN host; verify-full is opt-in via sslmode.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = context
    return kwargs


def open_backend(target: str | Path) -> SqliteBackend | PostgresBackend:
    """Choose a backend from a DSN or filesystem path."""
    value = str(target)
    backend = PostgresBackend(value) if is_postgres_dsn(value) else SqliteBackend(value)
    backend.migrate()
    return backend
