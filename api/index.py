"""Vercel entrypoint. All routes are served by one Python function.

Vercel's Python runtime discovers the module-level ASGI callable named `app`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from claude_monitor.serverless import app  # noqa: E402

__all__ = ["app"]
