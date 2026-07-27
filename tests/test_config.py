import os
import unittest
from unittest.mock import patch

from claude_monitor.config import Config, ConfigError, duration


REQUIRED = {name: "value" for name in (
    "ANTHROPIC_COMPLIANCE_ACCESS_KEY", "NOTION_TOKEN", "NOTION_DS_MEMBERS", "NOTION_DS_PROJECTS",
    "NOTION_DS_SYNC_RUNS", "NOTION_DS_CONVERSATIONS", "NOTION_DS_ACTIVITY")}


class ConfigTests(unittest.TestCase):
    def test_duration_units(self):
        self.assertEqual(duration("15m"), 900)
        self.assertEqual(duration("24h"), 86400)

    def test_required_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError): Config.from_env()

    def test_defaults_and_secret_values(self):
        with patch.dict(os.environ, REQUIRED, clear=True):
            config = Config.from_env()
        self.assertEqual(config.chat_page_limit, 100)
        self.assertFalse(config.download_attachments)
        self.assertEqual(config.compliance_access_key, "value")


if __name__ == "__main__": unittest.main()
