import unittest

from services.evidence_service import (
    bind_evidence,
    collect_evidence_segment_nos,
    merge_evidence,
    normalize,
)
from services.minutes_service import MinutesResult

SEGMENTS = [
    {"segment_no": 1, "start_ms": 0, "end_ms": 5000, "speaker": "张晨",
     "content": "报表导出改成异步导出，张涛负责后端。", "norm": normalize("报表导出改成异步导出，张涛负责后端。")},
    {"segment_no": 2, "start_ms": 5000, "end_ms": 9000, "speaker": "李悦",
     "content": "前端要加一个导出任务列表。", "norm": normalize("前端要加一个导出任务列表。")},
]


def make_result(quote: str) -> dict:
    return {
        "summary": "会议纪要",
        "topics": [],
        "viewpoints": [],
        "decisions": [
            {"content": "报表导出改为异步", "basis": "讨论", "owner_suggestion": "张涛",
             "deadline_suggestion": "", "evidence": [{"quote": quote}]}
        ],
        "pending_items": [],
        "risks": [],
    }


class EvidenceTest(unittest.TestCase):
    def test_normalize_removes_punctuation_and_space(self):
        self.assertEqual(normalize("报表导出，改成 异步！"), "报表导出改成异步")

    def test_bind_valid_quote_and_drop_invalid(self):
        result = bind_evidence(make_result("报表导出改成异步导出，张涛负责后端。"), SEGMENTS)
        evidence = result["decisions"][0]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["segment_no"], 1)
        self.assertEqual(evidence[0]["speaker"], "张晨")
        self.assertEqual(evidence[0]["start_ms"], 0)

        invalid = bind_evidence(make_result("这句话在转写里不存在"), SEGMENTS)
        self.assertEqual(invalid["decisions"][0]["evidence"], [])

    def test_too_short_quote_is_rejected(self):
        result = bind_evidence(make_result("是的"), SEGMENTS)
        self.assertEqual(result["decisions"][0]["evidence"], [])

    def test_merge_keeps_evidence_for_unchanged_item(self):
        old = bind_evidence(make_result("报表导出改成异步导出，张涛负责后端。"), SEGMENTS)
        new = make_result("报表导出改成异步导出，张涛负责后端。")
        new["decisions"][0]["evidence"] = []
        merged = merge_evidence(old, new, SEGMENTS)
        self.assertEqual(merged["decisions"][0]["evidence"][0]["segment_no"], 1)

    def test_merge_does_not_inherit_evidence_for_changed_item(self):
        old = bind_evidence(make_result("报表导出改成异步导出，张涛负责后端。"), SEGMENTS)
        new = make_result("完全不同的决策内容")
        new["decisions"][0]["content"] = "完全不同的决策内容"
        new["decisions"][0]["evidence"] = []
        merged = merge_evidence(old, new, SEGMENTS)
        self.assertEqual(merged["decisions"][0]["evidence"], [])

    def test_merge_without_segments_preserves_payload_evidence(self):
        old = {"topics": [], "viewpoints": [], "decisions": [], "pending_items": [], "risks": []}
        evidence = {"quote": "手动回传", "segment_no": 3, "start_ms": 1000, "end_ms": 2000, "speaker": "张三"}
        new = {"summary": "", "topics": [{"title": "t", "summary": "s", "evidence": [evidence]}]}
        merged = merge_evidence(old, new)
        self.assertEqual(merged["topics"][0]["evidence"][0], evidence)

    def test_minutes_result_accepts_evidence(self):
        result = MinutesResult.model_validate(
            {
                "summary": "摘要",
                "topics": [{"title": "议题", "summary": "总结", "evidence": [{"quote": "原文", "segment_no": 1}]}],
                "viewpoints": [],
                "decisions": [],
                "pending_items": [],
                "risks": [],
            }
        )
        self.assertEqual(result.topics[0].evidence[0].segment_no, 1)

    def test_collect_evidence_segment_nos(self):
        result = bind_evidence(make_result("报表导出改成异步导出，张涛负责后端。"), SEGMENTS)
        self.assertEqual(collect_evidence_segment_nos(result), {1})


if __name__ == "__main__":
    unittest.main()
