import os
import unittest
from unittest.mock import patch

from claude_monitor.config import Config, ConfigError, NotionConfig, duration


REQUIRED = {name: "value" for name in (
    "ANTHROPIC_COMPLIANCE_ACCESS_KEY", "NOTION_TOKEN", "NOTION_DS_MEMBERS", "NOTION_DS_PROJECTS",
    "NOTION_DS_SYNC_RUNS", "NOTION_DS_CONVERSATIONS", "NOTION_DS_ACTIVITY", "NOTION_PARENT_PAGE_ID")}
REQUIRED["PRODUCTION_READINESS"] = "employee-notice:complete,lawful-basis:complete,access-approval:complete"


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

    def test_production_readiness_fails_closed(self):
        values = dict(REQUIRED); values.pop("PRODUCTION_READINESS")
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ConfigError, "PRODUCTION_READINESS"): Config.from_env()

    def test_partial_readiness_is_rejected(self):
        values = dict(REQUIRED); values["PRODUCTION_READINESS"] = "employee-notice:complete"
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ConfigError, "explicitly confirm"): Config.from_env()

    def test_redaction_cannot_be_disabled(self):
        values = dict(REQUIRED); values["REDACTION_ENABLED"] = "false"
        with patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ConfigError, "cannot be disabled"): Config.from_env()

    def test_notion_check_configuration_is_independent(self):
        values = {key: value for key, value in REQUIRED.items() if key.startswith("NOTION_")}
        values["NOTION_DS_MESSAGES"] = "messages-id"
        with patch.dict(os.environ, values, clear=True):
            config = NotionConfig.from_env()
        self.assertEqual(config.data_sources["Messages"], "messages-id")
        self.assertNotIn("ANTHROPIC_COMPLIANCE_ACCESS_KEY", values)


if __name__ == "__main__": unittest.main()
