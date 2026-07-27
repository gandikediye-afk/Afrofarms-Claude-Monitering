"""Member attribution must survive redaction, on both ingestion planes.

The PII email pattern matches a plain member address, so redacting a whole
serialized payload empties the very fields that attribute a record to a person.
These tests pin the boundary: content is redacted, identity is not.
"""

import json
import sqlite3

from claude_monitor.normalizer import redact, redact_structure
from claude_monitor.otlp import _properties
from claude_monitor.state import State

EMAIL = "amina@afrofarms.example"


def event(**overrides):
    record = {
        "event_id": "evt-1", "event_type": "claude_code.user_prompt",
        "actor_email": EMAIL, "occurred_at": "2026-07-27T08:00:00Z",
        "surface": "Excel", "prompt_id": "p-1", "member_id": "user_01ABC",
        "ip_address": "192.0.2.9", "user_agent": "Excel/1.0",
        "attributes": {}, "cells_read": 400,
    }
    record.update(overrides)
    return record


def test_bare_member_email_matches_the_pii_pattern():
    """The premise: without an exemption, a plain address is redacted."""
    redacted, flags = redact(EMAIL)
    assert redacted == "[REDACTED PII]"
    assert "possible-pii" in flags


def test_staging_preserves_identity_fields(tmp_path):
    state = State(tmp_path / "state.db")
    state.stage("otel", "evt-1", event())
    stored = json.loads(state.queued("otel")[0]["payload"])
    assert stored["actor_email"] == EMAIL
    assert stored["member_id"] == "user_01ABC"
    assert stored["event_id"] == "evt-1"


def test_staging_still_redacts_content(tmp_path):
    """Identity is exempt; free text is not."""
    state = State(tmp_path / "state.db")
    state.stage("otel", "evt-2", event(
        event_id="evt-2",
        attributes={"note": f"reach out to supplier at broker@vendor.example",
                    "creds": "api_key=SUPERSECRETVALUE123"},
    ))
    payload = state.queued("otel")[0]["payload"]
    assert "broker@vendor.example" not in payload
    assert "SUPERSECRETVALUE123" not in payload
    assert EMAIL in payload, "the record's own subject must survive"


def test_activity_properties_carry_a_usable_email(tmp_path):
    """Notion's Actor Email is an email property: a redaction marker lands as null."""
    state = State(tmp_path / "state.db")
    state.stage("otel", "evt-1", event())
    stored = json.loads(state.queued("otel")[0]["payload"])
    props = _properties(stored, state)
    assert props["Actor Email"]["email"] == EMAIL
    assert "[REDACTED" not in props["Event"]["title"][0]["text"]["content"]


def test_redact_structure_walks_nested_payloads():
    payload = {"chat": {"user": {"email_address": EMAIL, "id": "user_01ABC"},
                        "name": "Budget with broker@vendor.example"},
               "messages": [{"content": [{"text": f"ping {EMAIL} and broker@vendor.example"}]}]}
    safe, flags = redact_structure(payload)
    assert safe["chat"]["user"]["email_address"] == EMAIL, "identity key exempt"
    assert safe["chat"]["user"]["id"] == "user_01ABC"
    assert "broker@vendor.example" not in safe["chat"]["name"], "content redacted"
    assert EMAIL not in safe["messages"][0]["content"][0]["text"], (
        "an address inside message content is content, not identity")
    assert "possible-pii" in flags


def test_redaction_can_be_disabled():
    payload = {"note": "broker@vendor.example"}
    safe, flags = redact_structure(payload, enabled=False)
    assert safe == payload
    assert flags == set()
