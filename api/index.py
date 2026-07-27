"""Vercel entrypoint: the simple Claude Office Agents -> Notion webhook.

No database, no cron jobs. Claude posts an event, it lands in Notion.
For the fuller pipeline (durable queue, chat sync) use
claude_monitor.serverless instead.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from claude_monitor.simple import app  # noqa: E402

__all__ = ["app"]
