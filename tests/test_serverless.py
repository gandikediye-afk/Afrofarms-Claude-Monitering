"""The serverless app: Postgres guard, cron auth, and worker-free ingest."""

import os

import pytest
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from starlette.testclient import TestClient

from claude_monitor.config import ConfigError
from claude_monitor.otlp import Settings
from claude_monitor.serverless import JOBS, create_app
from claude_monitor.state import State

DSN = os.environ.get("CLAUDE_MONITOR_TEST_DSN", "postgresql://postgres@127.0.0.1:5433/claude_monitor")


def settings(target, **kw):
    return Settings("secret-token-at-least-32-bytes-long", "authorization", 4096,
                    frozenset({"https://console.example"}), target,
                    kw.get("notion_token"), kw.get("notion_ds_activity"), 2.5,
                    kw.get("database_url"))


def envelope(event_id="evt-1"):
    return {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "Claude Code"}}]},
        "scopeLogs": [{"scope": {"name": "claude"}, "logRecords": [{
            "timeUnixNano": "1000000000",
            "body": {"stringValue": "private transcript"},
            "attributes": [
                {"key": "event.id", "value": {"stringValue": event_id}},
                {"key": "event.name", "value": {"stringValue": "tool.finished"}},
                {"key": "user.email", "value": {"stringValue": "amina@afrofarms.example"}},
            ]}]}]}]}


def protobuf(value):
    return ParseDict(value, ExportLogsServiceRequest()).SerializeToString()


def test_sqlite_is_refused_on_serverless(tmp_path):
    """The whole point of the port: an ephemeral disk must not be accepted."""
    with pytest.raises(ConfigError) as caught:
        create_app(settings(tmp_path / "state.db"))
    assert "DATABASE_URL" in str(caught.value)
    assert "duplicat" in str(caught.value)


def test_postgres_dsn_is_accepted():
    app = create_app(settings("/unused/state.db", database_url=DSN))
    assert app is not None


@pytest.fixture
def client():
    app = create_app(settings("/unused/state.db", database_url=DSN))
    with TestClient(app, base_url="https://testserver") as running:
        yield running


def test_cron_requires_the_secret(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    # An unconfigured deployment must not advertise open cron endpoints.
    assert client.post("/api/cron/chats").status_code == 401

    monkeypatch.setenv("CRON_SECRET", "cron-secret-value")
    assert client.post("/api/cron/chats").status_code == 401
    assert client.post("/api/cron/chats",
                       headers={"authorization": "Bearer wrong"}).status_code == 401


def test_unknown_cron_job_is_rejected(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value")
    response = client.post("/api/cron/rm-rf",
                           headers={"authorization": "Bearer cron-secret-value"})
    assert response.status_code == 404
    assert "unknown job" in response.json()["error"]


def test_every_declared_job_routes(client, monkeypatch):
    """Each job in JOBS must reach the handler, i.e. never 404."""
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value")
    for job in JOBS:
        response = client.post(f"/api/cron/{job}",
                               headers={"authorization": "Bearer cron-secret-value"})
        assert response.status_code != 404, job


def test_ingest_enqueues_durably_without_a_background_worker(client):
    headers = {"authorization": "Bearer secret-token-at-least-32-bytes-long",
               "content-type": "application/x-protobuf"}
    assert client.post("/v1/logs", content=protobuf(envelope()), headers=headers).status_code == 202

    reader = State(DSN)
    try:
        rows = reader.queued("otel")
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert "private transcript" not in payload, "OTLP body must not be persisted"
        assert "amina@afrofarms.example" in payload, "attribution must survive"
    finally:
        for row in list(reader.queued("otel")):
            reader.complete_item("otel", row["object_id"])
        reader.connection.close()


def test_health_is_public_and_ready_reflects_configuration(client):
    assert client.get("/health").status_code == 200
    # No Notion destination configured on this fixture: readiness must fail closed
    # so a platform health check does not route traffic to a half-configured
    # deployment that would silently drop events.
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json()["status"] == "not_ready"

    configured = create_app(settings("/unused/state.db", database_url=DSN,
                                     notion_token="ntn_mock", notion_ds_activity="ds_activity"))
    with TestClient(configured, base_url="https://testserver") as running:
        assert running.get("/ready").status_code == 200
        assert running.get("/ready").json()["status"] == "ready"
