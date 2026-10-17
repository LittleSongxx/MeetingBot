"""AI 复核脚本的单测：标签来源、一致性、AI-verified 判定；不调模型。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from gold import ai_review  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_intent_label_must_be_one_of_four(self):
        self.assertEqual(ai_review.normalize_intent_review(
            {"intent": "aggregation", "why": "列表"})["intent"], "aggregation")
        with self.assertRaises(ValueError):
            ai_review.normalize_intent_review({"intent": "procedure"})

    def test_extended_requires_two_booleans(self):
        self.assertTrue(ai_review.normalize_extended_review(
            {"worth_asking": True, "gold_correct": False})["worth_asking"])
        with self.assertRaises(ValueError):
            ai_review.normalize_extended_review({"worth_asking": "yes",
                                                  "gold_correct": True})


class ReviewIntentTest(unittest.TestCase):
    def test_labels_are_recorded_as_ai_review_with_agent_label_kept(self):
        qa = {"cases": [
            {"id": "q1", "question": "有哪些待办", "intent": "aggregation",
             "intent_source": "agent_draft"},
            {"id": "q2", "question": "上线时间", "intent": "single_fact",
             "intent_source": "agent_draft"},
        ]}
        with patch.object(ai_review, "_call", side_effect=[
                {"intent": "aggregation", "why": "列表"},
                {"intent": "aggregation", "why": "模型判错"}]):
            report = ai_review.review_intent(qa, url="", model="m", key="k",
                                             authorized_calls=2)
        self.assertEqual(report["reviewed"], 2)
        self.assertEqual(qa["cases"][0]["intent_source"], "ai_review")
        self.assertEqual(qa["cases"][0]["agent_draft_intent"], "aggregation")
        self.assertTrue(qa["cases"][0]["ai_review"]["agrees_with_agent_draft"])
        self.assertFalse(qa["cases"][1]["ai_review"]["agrees_with_agent_draft"])
        self.assertEqual(report["agreement"], 0.5)


class ReviewExtendedTest(unittest.TestCase):
    def test_both_yes_becomes_ai_verified(self):
        qa = {"cases": [
            {"id": "q1", "question": "问", "expected_facts": ["甲"],
             "source": {"task_id": 6, "segments": [1]}, "case_source": "agent_draft"},
            {"id": "q2", "question": "问2", "expected_facts": ["乙"],
             "source": {"task_id": 6, "segments": [2]}, "case_source": "agent_draft"},
        ]}
        segments = {6: {1: "甲在片段里", 2: "乙不在片段里"}}
        with patch.object(ai_review, "_call", side_effect=[
                {"worth_asking": True, "gold_correct": True, "why": "好"},
                {"worth_asking": True, "gold_correct": False, "why": "金标不准"}]):
            report = ai_review.review_extended(qa, url="", model="m", key="k",
                                               segments=segments, authorized_calls=2)
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["rejected"], 1)
        self.assertEqual(qa["cases"][0]["case_source"], "ai_verified")
        self.assertEqual(qa["cases"][1]["case_source"], "agent_draft")
        self.assertFalse(qa["cases"][1]["ai_review"]["gold_correct"])


if __name__ == "__main__":
    unittest.main()
