import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from claude_monitor.normalizer import redact
from claude_monitor.retention import RetentionWorker
from claude_monitor.state import State


FIXTURE = json.loads((Path(__file__).parent / "fixtures/governance_cases.json").read_text())


def prop(kind, value):
    if kind == "date": value = {"start": value}
    elif kind == "select": value = {"name": value}
    elif kind in {"title", "rich_text"}: value = [{"plain_text": value}]
    return {"type": kind, kind: value}


class FakeNotion:
    def __init__(self, pages): self.pages, self.calls = pages, []
    def query_data_source(self, data_source_id, **kwargs):
        self.calls.append(("query", data_source_id, kwargs)); return {"results": self.pages, "has_more": False}
    def strip_blocks(self, page_id): self.calls.append(("strip", page_id))
    def update_page(self, page_id, properties, **kwargs): self.calls.append(("update", page_id, properties, kwargs))


def config(**overrides):
    values = dict(notion_ds_conversations="conversations", notion_ds_messages=None,
                  deletion_grace_period=7 * 86400, retention_class_days={"standard": 365})
    values.update(overrides); return SimpleNamespace(**values)


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = State(Path(self.tmp.name) / "state.db")
    def tearDown(self): self.state.connection.close(); self.tmp.cleanup()

    def test_synthetic_secrets_and_pii_are_redacted_before_sqlite(self):
        safe, flags = redact(FIXTURE["synthetic_content"])
        self.assertNotIn("SYNTHETICONLY", safe); self.assertNotIn("123456789", safe)
        self.assertEqual({"possible-secret", "possible-pii", "redacted"}, flags)
        self.state.stage("chats", "one", {"raw": FIXTURE["synthetic_content"]})
        stored = self.state.queued("chats")[0]["payload"]
        self.assertNotIn("SYNTHETICONLY", stored); self.assertIn("[REDACTED SECRET]", stored)

    def test_retention_and_soft_delete_tombstone_and_archive(self):
        pages = [
            {"id": "expired", "properties": {"Chat ID": prop("rich_text", "chat-1"), "Last Activity": prop("date", "2024-01-01T00:00:00Z"), "Retention Class": prop("select", "standard")}},
            {"id": "deleted", "properties": {"Chat ID": prop("rich_text", "chat-2"), "Deleted At": prop("date", "2026-01-01T00:00:00Z"), "Retention Class": prop("select", "standard")}},
        ]
        notion = FakeNotion(pages)
        count = RetentionWorker(notion, self.state, config()).run_once(now=datetime(2026, 7, 27, tzinfo=timezone.utc))
        self.assertEqual(count, 2)
        self.assertEqual(["deleted", "expired"], sorted(call[1] for call in notion.calls if call[0] == "strip"))
        updates = [call for call in notion.calls if call[0] == "update"]
        self.assertTrue(all(call[2]["Tombstoned"]["checkbox"] and call[3]["archived"] for call in updates))
        self.assertEqual(2, self.state.connection.execute("SELECT count(*) FROM governance_actions").fetchone()[0])

    def test_legal_hold_is_exempt(self):
        page = {"id": "held", "properties": {"Chat ID": prop("rich_text", "chat-held"), "Last Activity": prop("date", "2020-01-01T00:00:00Z"), "Retention Class": prop("select", "legal-hold")}}
        notion = FakeNotion([page])
        self.assertEqual(0, RetentionWorker(notion, self.state, config()).run_once(now=datetime(2026, 7, 27, tzinfo=timezone.utc)))
        self.assertFalse(any(call[0] in {"strip", "update"} for call in notion.calls))

    def test_duplicate_action_delivery_is_recorded_once(self):
        self.assertTrue(self.state.record_governance_action("page", "chat", "tombstone", "hard-delete"))
        self.assertFalse(self.state.record_governance_action("page", "chat", "tombstone", "hard-delete"))


if __name__ == "__main__": unittest.main()
