import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from claude_monitor.anthropic_client import AnthropicClient
from claude_monitor.cli import main


class Response(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ComplianceCheckTests(unittest.TestCase):
    def test_check_uses_key_and_minimal_chat_request(self):
        client = AnthropicClient("very-secret-key", max_retries=0)
        seen = {}

        def open_request(request, timeout):
            seen.update(url=request.full_url, key=request.get_header("X-api-key"), timeout=timeout)
            return Response(json.dumps({"data": [], "has_more": False}).encode())

        with patch.object(client._opener, "open", side_effect=open_request):
            result = client.compliance_check()
        self.assertTrue(result.ok)
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/compliance/apps/chats?limit=1")
        self.assertEqual(seen["key"], "very-secret-key")

    def test_403_explains_key_type_and_scope_without_exposing_key(self):
        key = "never-print-this"
        client = AnthropicClient(key, max_retries=0)
        error = urllib.error.HTTPError("url", 403, "forbidden", {}, io.BytesIO(key.encode()))
        with patch.object(client._opener, "open", side_effect=error):
            result = client.compliance_check()
        self.assertFalse(result.ok)
        self.assertIn("wrong key type", result.message)
        self.assertIn("read scope", result.message)
        self.assertNotIn(key, result.message)

    def test_unavailable_endpoint_explains_access_not_enabled(self):
        client = AnthropicClient("key", max_retries=0)
        error = urllib.error.HTTPError("url", 404, "missing", {}, io.BytesIO(b"missing"))
        with patch.object(client._opener, "open", side_effect=error):
            result = client.compliance_check()
        self.assertIn("not enabled", result.message)

    @patch("claude_monitor.cli.AnthropicClient")
    def test_cli_only_requires_compliance_environment(self, client_type):
        client_type.return_value.compliance_check.return_value.ok = True
        client_type.return_value.compliance_check.return_value.message = "HTTP 200"
        output = io.StringIO()
        with patch.dict(os.environ, {"ANTHROPIC_COMPLIANCE_ACCESS_KEY": "secret"}, clear=True), patch("sys.stdout", output):
            self.assertEqual(main(["compliance", "check"]), 0)
        self.assertNotIn("secret", output.getvalue())

    def test_rejects_non_origin_base_url(self):
        with self.assertRaisesRegex(ValueError, "must not contain a path"):
            AnthropicClient("key", "https://api.anthropic.com/proxy")


if __name__ == "__main__":
    unittest.main()
