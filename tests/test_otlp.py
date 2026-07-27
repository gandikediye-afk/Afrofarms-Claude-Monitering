import json
import sqlite3

from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from starlette.testclient import TestClient

from claude_monitor.otlp import Settings, create_app, normalize


def envelope(event_id="evt-1"):
    return {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "Claude Code"}}
    ]}, "scopeLogs": [{"scope": {"name": "claude"}, "logRecords": [{
        "timeUnixNano": "1000000000", "body": {"stringValue": "private transcript"},
        "attributes": [
            {"key": "event.id", "value": {"stringValue": event_id}},
            {"key": "event.name", "value": {"stringValue": "tool.finished"}},
            {"key": "prompt.id", "value": {"stringValue": "prompt-1"}},
            {"key": "user.id", "value": {"stringValue": "user-1"}},
            {"key": "sheet.cells_read", "value": {"intValue": "4"}},
        ]}]}]}]}


def settings(tmp_path):
    return Settings("secret", "authorization", 4096, frozenset({"https://console.example"}),
                    tmp_path / "state.db", None, None, 2.5)


def protobuf(value):
    return ParseDict(value, ExportLogsServiceRequest()).SerializeToString()


def test_normalization_drops_body_and_extracts_metadata():
    record = normalize(envelope())[0]
    assert record["event_id"] == "evt-1"
    assert record["prompt_id"] == "prompt-1"
    assert record["member_id"] == "user-1"
    assert record["surface"] == "Claude Code"
    assert record["cells_read"] == 4
    assert "private transcript" not in json.dumps(record)


def test_ingest_requires_https_auth_and_durably_deduplicates(tmp_path):
    with TestClient(create_app(settings(tmp_path)), base_url="https://testserver") as client:
        assert client.post("/v1/logs", json=envelope()).status_code == 401
        headers = {"authorization": "Bearer secret", "content-type": "application/x-protobuf"}
        assert client.post("/v1/logs", content=protobuf(envelope()), headers=headers).status_code == 202
        assert client.post("/v1/logs", content=protobuf(envelope()), headers=headers).status_code == 202
        # TestClient runs the app's event loop on its own thread, and that is the
        # thread that opened app.state.db. Reading that connection from here would
        # raise ProgrammingError -- an artifact of the harness, not of the service,
        # whose ingest path only ever touches SQLite from the loop thread. Read the
        # committed rows through an independent connection instead.
        rows = list(sqlite3.connect(tmp_path / "state.db").execute(
            "SELECT object_id, payload FROM work_queue WHERE plane='otel'"))
        assert len(rows) == 1
        assert "private transcript" not in rows[0][1]


def test_preflight_is_allowlisted_and_size_is_enforced(tmp_path):
    with TestClient(create_app(settings(tmp_path)), base_url="https://testserver") as client:
        assert client.options("/v1/logs", headers={"origin": "https://evil.example"}).status_code == 403
        allowed = client.options("/v1/logs", headers={"origin": "https://console.example"})
        assert allowed.status_code == 204
        response = client.post("/v1/logs", content=b"x" * 4097,
                               headers={"authorization": "Bearer secret", "content-type": "application/x-protobuf"})
        assert response.status_code == 413


def test_plain_http_is_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path)), base_url="http://testserver") as client:
        response = client.post("/v1/logs", content=protobuf(envelope()),
                               headers={"authorization": "Bearer secret", "content-type": "application/x-protobuf"})
        assert response.status_code == 400
