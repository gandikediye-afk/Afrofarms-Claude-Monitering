"""The no-database webhook: auth, decode, dedupe, and the Notion write."""

import json

import pytest
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from starlette.testclient import TestClient

from claude_monitor import simple
from claude_monitor.simple import Config, create_app

from .mockapi import MockNotion

SECRET = "secret-token-at-least-32-bytes-long!"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("OTLP_SHARED_SECRET", SECRET)
    monkeypatch.setenv("NOTION_TOKEN", "ntn_mock")
    monkeypatch.setenv("NOTION_DS_ACTIVITY", "ds_activity")
    monkeypatch.setenv("OTLP_ALLOWED_ORIGINS", "https://console.example")
    return Config()


@pytest.fixture
def notion(monkeypatch):
    api = MockNotion()
    real = simple.NotionClient
    monkeypatch.setattr(simple, "NotionClient",
                        lambda token, *a, **k: real(token, 1000.0, opener=api.open))
    return api


@pytest.fixture
def client(config, notion):
    with TestClient(create_app(config), base_url="https://testserver") as running:
        yield running


def envelope(event_id="evt-1", app_name="Excel"):
    return {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": app_name}}]},
        "scopeLogs": [{"scope": {"name": "claude"}, "logRecords": [{
            "timeUnixNano": "1775808550000000000",
            "body": {"stringValue": "What were our Q3 avocado margins?"},
            "attributes": [
                {"key": "event.id", "value": {"stringValue": event_id}},
                {"key": "event.name", "value": {"stringValue": "claude_code.user_prompt"}},
                {"key": "user.email", "value": {"stringValue": "amina@afrofarms.example"}},
                {"key": "sheet.cells_read", "value": {"intValue": "412"}},
            ]}]}]}]}


def protobuf(value):
    return ParseDict(value, ExportLogsServiceRequest()).SerializeToString()


def auth(**extra):
    return {"authorization": f"Bearer {SECRET}", **extra}


def test_missing_configuration_is_reported_by_name(monkeypatch):
    for name in ("OTLP_SHARED_SECRET", "NOTION_TOKEN", "NOTION_DS_ACTIVITY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as caught:
        Config()
    for name in ("OTLP_SHARED_SECRET", "NOTION_TOKEN", "NOTION_DS_ACTIVITY"):
        assert name in str(caught.value)


def test_short_secret_is_refused(monkeypatch):
    monkeypatch.setenv("OTLP_SHARED_SECRET", "too-short")
    monkeypatch.setenv("NOTION_TOKEN", "ntn_mock")
    monkeypatch.setenv("NOTION_DS_ACTIVITY", "ds_activity")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        Config()


def test_event_becomes_a_notion_row(client, notion):
    response = client.post("/v1/logs", content=protobuf(envelope()),
                           headers=auth(**{"content-type": "application/x-protobuf"}))
    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "created": 1, "skipped": 0}

    pages = notion.pages_in("ds_activity")
    assert len(pages) == 1
    props = notion.pages[pages[0]]["properties"]
    assert props["Actor Email"] == {"email": "amina@afrofarms.example"}
    assert props["Surface"]["select"]["name"] == "Excel"
    assert props["Cells Read"]["number"] == 412
    assert props["Event ID"]["rich_text"][0]["text"]["content"] == "evt-1"


def test_chat_text_is_never_written(client, notion):
    client.post("/v1/logs", content=protobuf(envelope()),
                headers=auth(**{"content-type": "application/x-protobuf"}))
    stored = json.dumps(notion.pages)
    assert "avocado" not in stored, "OTLP bodies must not reach Notion"


def test_json_encoding_is_accepted(client, notion):
    response = client.post("/v1/logs", json=envelope("evt-json"),
                           headers=auth(**{"content-type": "application/json"}))
    assert response.status_code == 202
    assert len(notion.pages_in("ds_activity")) == 1


def test_redelivery_does_not_duplicate(client, notion):
    headers = auth(**{"content-type": "application/x-protobuf"})
    first = client.post("/v1/logs", content=protobuf(envelope()), headers=headers)
    assert first.json()["created"] == 1

    notion.query_results = [{"id": "page_0001"}]  # Notion now reports the event exists
    second = client.post("/v1/logs", content=protobuf(envelope()), headers=headers)
    assert second.json() == {"accepted": 1, "created": 0, "skipped": 1}
    assert len(notion.pages_in("ds_activity")) == 1


def test_auth_and_content_type_failures(client):
    assert client.post("/v1/logs", content=protobuf(envelope()),
                       headers={"authorization": "Bearer wrong",
                                "content-type": "application/x-protobuf"}).status_code == 401
    assert client.post("/v1/logs", content=b"x",
                       headers=auth(**{"content-type": "text/csv"})).status_code == 415


def test_cors_preflight(client):
    assert client.options("/v1/logs", headers={"origin": "https://evil.example"}).status_code == 403
    allowed = client.options("/v1/logs", headers={"origin": "https://console.example"})
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "https://console.example"


def test_notion_failure_returns_503_so_the_exporter_retries(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("notion down")
    monkeypatch.setattr(simple, "_write", boom)
    response = client.post("/v1/logs", content=protobuf(envelope()),
                           headers=auth(**{"content-type": "application/x-protobuf"}))
    assert response.status_code == 503


def test_health_lists_accepted_encodings(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "application/json" in body["accepts"]
    assert "application/x-protobuf" in body["accepts"]
