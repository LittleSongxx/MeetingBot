"""意图路由规则单测（纯函数，零模型）。"""
from __future__ import annotations

import unittest

from services import intent_router


class IntentRouterTest(unittest.TestCase):
    def test_cross_meeting_wins_over_aggregation(self):
        self.assertEqual(
            intent_router.classify("发布公告与对外通稿分别由谁负责、什么时候完成？"),
            "cross_meeting")

    def test_state_action_items(self):
        self.assertEqual(
            intent_router.classify("下午这场会定了哪些行动项和负责人？"), "state")

    def test_aggregation_list(self):
        self.assertEqual(
            intent_router.classify("正式发布前还要补充哪两组验收数据？"),
            "aggregation")

    def test_smalltalk(self):
        self.assertEqual(intent_router.classify("你好，你能帮我做什么？"), "smalltalk")

    def test_policy_budget(self):
        policy = intent_router.policy_for("两段录音分别说了什么？")
        self.assertEqual(policy["intent"], "cross_meeting")
        self.assertEqual(policy["first_tool"], "search_knowledge")
        self.assertGreater(policy["max_tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
