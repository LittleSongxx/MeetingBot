import unittest
from unittest.mock import patch

from services import chat_turn


class FakeRegistry:
    def __init__(self, contexts):
        self._contexts = contexts

    def as_dicts(self):
        return self._contexts


class ApplyCitationBindingTest(unittest.TestCase):
    def test_disabled_by_default_returns_unchanged(self):
        answer = "时间是 9月1日 [1]。"
        registry = FakeRegistry([{"ref": 1, "source_text": "无关"},
                                 {"ref": 2, "source_text": "9月1日 发布"}])
        with patch.dict("os.environ", {}, clear=True):
            bound, summary = chat_turn.apply_citation_binding(answer, registry)
        self.assertEqual(bound, answer)
        self.assertIsNone(summary)

    def test_enabled_rebinds_and_returns_summary(self):
        answer = "时间是 9月1日 [1]。"
        registry = FakeRegistry([{"ref": 1, "source_text": "无关"},
                                 {"ref": 2, "source_text": "9月1日 发布"}])
        with patch.dict("os.environ", {"CHAT_CITATION_BIND_ENABLED": "1"}):
            bound, summary = chat_turn.apply_citation_binding(answer, registry)
        self.assertIn("[2]", bound)
        self.assertEqual(summary["rebound"], 1)
        self.assertTrue(summary["changed"])


if __name__ == "__main__":
    unittest.main()
