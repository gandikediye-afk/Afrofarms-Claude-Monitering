import unittest

from claude_monitor.normalizer import canonical_chat, normalize_messages, transcript_blocks


class NormalizerTests(unittest.TestCase):
    def test_redacts_before_canonicalizing_and_counts_metadata(self):
        messages = [{"id": "2", "role": "assistant", "created_at": "b", "content": [{"type": "text", "text": "ok"}]},
                    {"id": "1", "role": "user", "created_at": "a", "content": [{"type": "text", "text": "token=abcdefghijk user@example.com"}],
                     "files": [{"id": "f1", "filename": "x.pdf", "mime_type": "application/pdf", "signed_url": "never persist"}]}]
        value = normalize_messages(messages)
        self.assertEqual(value["roles"], {"user": 1, "assistant": 1})
        self.assertEqual(value["attachments"], 1)
        self.assertNotIn("abcdefghijk", str(value))
        self.assertNotIn("signed_url", str(value))
        self.assertIn("possible-secret", value["flags"])
        canonical, digest = canonical_chat({"id": "c", "deleted_at": None}, value)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, canonical_chat({"id": "c", "deleted_at": None}, value)[1])
        self.assertGreaterEqual(len(transcript_blocks(value)), 4)


if __name__ == "__main__": unittest.main()
