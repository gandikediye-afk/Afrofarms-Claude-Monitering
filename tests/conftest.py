"""Optionally run the whole suite against Postgres.

    CLAUDE_MONITOR_TEST_DSN=postgresql://... pytest

Every test gets its own Postgres schema, so path-based `State(tmp_path/...)`
isolation carries over unchanged and the same assertions run on both backends.
"""

from __future__ import annotations

import hashlib
import os

import pytest


def _schema_for(nodeid: str) -> str:
    return "t_" + hashlib.sha1(nodeid.encode()).hexdigest()[:24]


@pytest.fixture(autouse=True)
def state_backend(request, monkeypatch):
    dsn = os.environ.get("CLAUDE_MONITOR_TEST_DSN")
    if not dsn:
        yield "sqlite"
        return

    from claude_monitor import db as db_module

    schema = _schema_for(request.node.nodeid)

    class ScopedPostgres(db_module.PostgresBackend):
        """Pins every connection this test opens to one throwaway schema."""

        def __init__(self, _target):
            super().__init__(dsn)
            self.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            self.execute(f"SET search_path TO {schema}")

    def open_backend(target):
        backend = ScopedPostgres(target)
        backend.migrate()
        return backend

    monkeypatch.setattr(db_module, "open_backend", open_backend)
    monkeypatch.setattr("claude_monitor.state.open_backend", open_backend)

    yield "postgres"

    cleanup = db_module.PostgresBackend(dsn)
    try:
        cleanup.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        cleanup.close()
