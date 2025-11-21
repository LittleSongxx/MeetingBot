"""首轮工具选择诊断的单测：strict / acceptable、按意图、缺产物。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from regression import tool_choice  # noqa: E402


def _gold():
    return {"cases": [
        {"case_id": "q1", "question": "有哪些待办", "intent": "aggregation",
         "expected_first_tool": "list_facts",
         "acceptable_first_tools": ["list_facts", "search_knowledge"]},
        {"case_id": "q2", "question": "上线时间", "intent": "single_fact",
         "expected_first_tool": "search_knowledge",
         "acceptable_first_tools": ["search_knowledge"]},
        {"case_id": "q3", "question": "两段分别怎样", "intent": "cross_meeting",
         "expected_first_tool": "search_knowledge",
         "acceptable_first_tools": ["search_knowledge"]},
    ]}


def _artifact():
    return {"turns": [
        {"question": "有哪些待办", "tools": [{"name": "search_knowledge"}]},
        {"question": "上线时间", "tools": [{"name": "search_knowledge"}]},
        {"question": "两段分别怎样", "tools": [{"name": "list_facts"}]},
    ]}


class SummarizeTest(unittest.TestCase):
    def test_strict_and_acceptable_are_reported_separately(self):
        summary = tool_choice.summarize(_gold(), [_artifact()])
        self.assertEqual(summary["paired"], 3)
        self.assertEqual(summary["strict_accuracy"], 1 / 3, "aggregation/cross 用错工具不算 strict")
        self.assertEqual(summary["acceptable_accuracy"], 2 / 3,
                         "aggregation 用 search 可接受；cross 用 list_facts 不可接受")
        self.assertEqual(summary["per_intent"]["aggregation"]["support"], 1)
        self.assertEqual(summary["per_intent"]["aggregation"]["strict_accuracy"], 0.0)

    def test_missing_turn_is_listed_not_scored(self):
        artifact = {"turns": [{"question": "上线时间", "tools": [{"name": "search_knowledge"}]}]}
        summary = tool_choice.summarize(_gold(), [artifact])
        self.assertEqual(summary["paired"], 1)
        self.assertEqual(sorted(summary["missing"]), ["q1", "q3"])

    def test_no_tools_is_only_acceptable_when_expected_is_none(self):
        gold = {"cases": [{"case_id": "s1", "question": "你好", "intent": "smalltalk",
                           "expected_first_tool": None, "acceptable_first_tools": []}]}
        artifact = {"turns": [{"question": "你好", "tools": []}]}
        summary = tool_choice.summarize(gold, [artifact])
        self.assertEqual(summary["strict_accuracy"], 1.0)
        self.assertEqual(summary["acceptable_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
