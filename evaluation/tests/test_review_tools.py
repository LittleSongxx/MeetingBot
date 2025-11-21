"""人工复核导入与意图审计的单测：不猜、不覆盖原文件、空行保持原状。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from gold import audit_intent, import_review  # noqa: E402


def _qa():
    return {"cases": [
        {"id": "q1", "question": "有哪些待办", "intent": "aggregation",
         "intent_source": "agent_draft"},
        {"id": "q2", "question": "上线时间", "intent": "single_fact",
         "intent_source": "agent_draft"},
        {"id": "n1", "question": "预算多少", "unanswerable": True},
    ]}


class ImportReviewTest(unittest.TestCase):
    def test_intent_rows_are_applied_only_when_a_valid_label_is_present(self):
        rows = [
            {"anonymous_id": "q1", "label": "aggregation", "note": "人工确认"},
            {"anonymous_id": "q2", "label": ""},
            {"anonymous_id": "n1", "label": "single_fact"},
            {"anonymous_id": "q2", "label": "not_a_label"},
        ]
        qa = _qa()
        report = import_review.apply_intent(qa, rows)
        self.assertEqual(report["updated"], 1)
        self.assertEqual(qa["cases"][0]["intent_source"], "human")
        self.assertEqual(qa["cases"][0]["intent_review_note"], "人工确认")
        self.assertEqual(qa["cases"][1]["intent_source"], "agent_draft")
        self.assertEqual(report["blank_rows"], 1)
        self.assertEqual(report["unknown_cases"], 1)
        self.assertEqual(len(report["unknown_labels"]), 1)

    def test_extended_requires_both_yes_to_become_human(self):
        qa = {"cases": [
            {"id": "q1", "case_source": "agent_draft"},
            {"id": "q2", "case_source": "agent_draft"},
            {"id": "q3", "case_source": "agent_draft"},
        ]}
        rows = [
            {"case_id": "q1", "human_worth_asking": "是", "human_gold_correct": "yes"},
            {"case_id": "q2", "human_worth_asking": "否", "human_gold_correct": "是"},
            {"case_id": "q3", "human_worth_asking": "", "human_gold_correct": ""},
        ]
        report = import_review.apply_extended(qa, rows)
        self.assertEqual(report["human_verified"], 1)
        self.assertEqual(report["human_rejected"], 1)
        self.assertEqual(qa["cases"][0]["case_source"], "human")
        self.assertEqual(qa["cases"][1]["case_source"], "agent_draft")
        self.assertEqual(qa["cases"][1]["human_review"]["worth_asking"], "否")


class AuditIntentTest(unittest.TestCase):
    def test_marker_and_layout_conflicts_are_flagged(self):
        qa = {"cases": [
            {"id": "q1", "question": "有哪些待办", "intent": "single_fact"},
            {"id": "q2", "question": "上线时间", "intent": "aggregation"},
            {"id": "q3", "question": "两段分别怎样", "intent": "cross_meeting"},
            {"id": "q4", "question": "两段分别怎样", "intent": "single_fact",
             "also_in": {"task_id": 7, "segments": [1]}},
            {"id": "q5", "question": "上线时间是什么", "intent": "single_fact"},
        ]}
        report = audit_intent.audit(qa)
        ids = {item["case_id"] for item in report["items"]}
        self.assertEqual(ids, {"q1", "q2", "q3", "q4"})
        self.assertEqual(report["checked"], 5)


if __name__ == "__main__":
    unittest.main()
