import io
import os
import unittest
import urllib.error
from unittest.mock import patch

from claude_monitor.cli import _discover, main
from claude_monitor.notion_client import NotionClient, NotionError, SchemaError
from claude_monitor.writer import SCHEMAS, check_notion_config


SOURCES = {
    "Members": "members", "Projects": "projects", "Sync Runs": "runs",
    "Conversations": "chats", "Agent Activity": "activity",
}


class NotionCheckTests(unittest.TestCase):
    @staticmethod
    def database(title, source_id):
        return {
            "title": [{"plain_text": title}],
            "data_sources": [{"id": source_id, "name": title}],
        }

    def test_checks_all_sources_and_reports_schema_errors(self):
        class FakeNotion:
            def validate_data_source(self, name, source_id, schema):
                self.calls.append((name, source_id, schema))

            def __init__(self):
                self.calls = []

        notion = FakeNotion()
        results = check_notion_config(notion, SOURCES)
        self.assertTrue(all(error is None for _, error in results))
        self.assertEqual(len(notion.calls), 5)
        self.assertIs(notion.calls[0][2], SCHEMAS["members"])

    def test_unshared_database_error_names_database(self):
        client = NotionClient("secret", opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("url", 404, "not found", {}, io.BytesIO(b'{"message":"not found"}'))
        ))
        with self.assertRaisesRegex(SchemaError, "Members is not reachable.*share that database"):
            client.validate_data_source("Members", "bad-id", SCHEMAS["members"])

    def test_api_errors_redact_token(self):
        token = "secret-token-value"
        client = NotionClient(token, opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("url", 400, "bad", {}, io.BytesIO(token.encode()))
        ))
        with self.assertRaises(NotionError) as raised:
            client.data_source("id")
        self.assertNotIn(token, str(raised.exception))

    @patch("claude_monitor.cli.check_notion_config", return_value=[("Members", None)])
    @patch("claude_monitor.cli.NotionClient")
    def test_cli_command_uses_only_notion_environment(self, _client, _check):
        environment = {"NOTION_TOKEN": "secret", **{
            f"NOTION_DS_{name}": value for name, value in {
                "MEMBERS": "m", "PROJECTS": "p", "SYNC_RUNS": "s",
                "CONVERSATIONS": "c", "ACTIVITY": "a",
            }.items()
        }}
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch("sys.stdout", output):
            self.assertEqual(main(["notion", "check"]), 0)
        self.assertEqual(output.getvalue(), "OK  Members\n")
        self.assertNotIn("secret", output.getvalue())

    def test_discover_parent_matches_documented_titles(self):
        databases = [
            self.database("Team Members", "members"),
            self.database("Claude Projects", "projects"),
            self.database("Sync Runs", "runs"),
            self.database("Conversations", "chats"),
            self.database("Agent Activity", "activity"),
        ]

        class FakeNotion:
            def child_databases(self, page_id):
                self.page_id = page_id
                return databases

        notion = FakeNotion()
        parent, sources = _discover(notion, ["01234567-89ab-cdef-0123-456789abcdef"])
        self.assertEqual(parent, "0123456789abcdef0123456789abcdef")
        self.assertEqual(notion.page_id, parent)
        self.assertEqual(sources["NOTION_DS_MEMBERS"], "members")

    def test_discover_database_urls_and_rejects_duplicate_titles(self):
        class FakeNotion:
            def database(self, database_id):
                return NotionCheckTests.database("Team Members", database_id)

        with self.assertRaisesRegex(ValueError, "duplicate Notion database title 'Team Members'"):
            _discover(FakeNotion(), [
                "https://www.notion.so/Team-Members-0123456789abcdef0123456789abcdef",
                "https://www.notion.so/Team-Members-fedcba9876543210fedcba9876543210",
            ])

    @patch("claude_monitor.cli.check_notion_config", return_value=[("Members", None)])
    @patch("claude_monitor.cli._discover", return_value=("parent", {"NOTION_DS_MEMBERS": "members"}))
    @patch("claude_monitor.cli.NotionClient")
    def test_discover_cli_masks_token_and_runs_check(self, _client, _discover_call, check):
        output = io.StringIO()
        with patch.dict(os.environ, {"NOTION_TOKEN": "secret-token"}, clear=True), patch("sys.stdout", output):
            self.assertEqual(main(["notion", "discover", "0123456789abcdef0123456789abcdef"]), 0)
        self.assertIn("NOTION_TOKEN=[REDACTED]", output.getvalue())
        self.assertIn("NOTION_DS_MEMBERS=members", output.getvalue())
        self.assertIn("OK  Members", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
