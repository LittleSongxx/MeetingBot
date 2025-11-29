"""多时间点检索深度策略（零模型调用）。"""
import os
import unittest
from unittest.mock import patch

from services import kb_service, retrieval_policy


class RetrievalPolicyTest(unittest.TestCase):
    def test_disabled_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(retrieval_policy.adaptive_depth(
                "8月和9月分别有哪些时间节点？"))

    def test_multi_time_or_timeline_cue_triggers(self):
        with patch.dict(os.environ, {retrieval_policy.ENV: "1"}):
            self.assertEqual(retrieval_policy.adaptive_depth(
                "8月20日灰度、8月26日扩量，关键时间节点是什么？"), 5)
            self.assertEqual(retrieval_policy.adaptive_depth(
                "从灰度到正式发布的时间线是怎样的？"), 5)
            self.assertIsNone(retrieval_policy.adaptive_depth(
                "发布公告由谁负责？"))

    def test_top_k_override_is_context_local(self):
        kb_service.clear_top_k_override()
        with patch.dict(os.environ, {"RERANK_TOP_K": "3"}):
            self.assertEqual(kb_service.rerank_top_k(), 3)
            kb_service.set_top_k_override(5)
            self.assertEqual(kb_service.rerank_top_k(), 5)
            kb_service.clear_top_k_override()
            self.assertEqual(kb_service.rerank_top_k(), 3)


if __name__ == "__main__":
    unittest.main()
