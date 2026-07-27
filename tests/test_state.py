import tempfile
import unittest
from pathlib import Path

from claude_monitor.state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "state.db")

    def tearDown(self):
        self.state.connection.close(); self.temp.cleanup()

    def test_cursor_advances_only_after_queue_drains(self):
        self.state.stage("chats", "c1", {"safe": True})
        with self.assertRaises(RuntimeError): self.state.finish_walk("chats", "opaque")
        self.assertIsNone(self.state.get_cursor("chats"))
        self.state.complete_item("chats", "c1"); self.state.finish_walk("chats", "opaque")
        self.assertEqual(self.state.get_cursor("chats"), "opaque")

    def test_indexes_and_exclusive_claim(self):
        self.state.put_chat("c1", "p1", "hash", 3, None)
        self.assertEqual(self.state.chat("c1")["notion_page_id"], "p1")
        self.assertTrue(self.state.claim("chat", "c2"))
        self.assertFalse(self.state.claim("chat", "c2"))
        self.state.release("chat", "c2")


if __name__ == "__main__": unittest.main()
