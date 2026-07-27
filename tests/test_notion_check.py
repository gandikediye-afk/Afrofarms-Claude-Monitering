import io
import os
import unittest
import urllib.error
from unittest.mock import patch

from claude_monitor.cli import main
from claude_monitor.notion_client import NotionClient, NotionError, SchemaError
from claude_monitor.writer import SCHEMAS, check_notion_config


SOURCES = {
    "Members": "members", "Projects": "projects", "Sync Runs": "runs",
    "Conversations": "chats", "Agent Activity": "activity",
}


class NotionCheckTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
