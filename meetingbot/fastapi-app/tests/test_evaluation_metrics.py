import unittest

from evaluation.metrics import (
    action_match,
    edit_stats,
    evidence_stats,
    field_coverage,
    text_similarity,
)


class EvaluationMetricsTest(unittest.TestCase):
    def test_field_coverage(self):
        result = field_coverage(
            {"summary": "有", "topics": [], "viewpoints": [{"speaker": "A"}], "decisions": [], "pending_items": [], "risks": []}
        )
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["covered"], 2)

    def test_evidence_stats_validates_quotes(self):
        payload = {
            "topics": [
                {"title": "t", "summary": "s", "evidence": [{"segment_no": 1, "quote": "报表导出改成异步"}]},
                {"title": "t2", "summary": "s2", "evidence": [{"segment_no": 1, "quote": "编造的引用"}]},
            ]
        }
        segments = {1: {"content": "报表导出改成异步导出，张涛负责后端。"}}
        result = evidence_stats(payload, segments)
        self.assertEqual(result["items_with_evidence"], 2)
        self.assertEqual(result["total_evidence"], 2)
        self.assertEqual(result["valid_evidence"], 1)

    def test_edit_stats(self):
        ai_draft = {
            "summary": "原始摘要",
            "topics": [{"title": "A", "summary": "内容"}],
            "decisions": [{"content": "决策一"}],
        }
        final = {
            "summary": "人工改过的摘要",
            "topics": [{"title": "A", "summary": "内容"}],
            "decisions": [{"content": "决策一"}, {"content": "新增决策"}],
        }
        stats = edit_stats(ai_draft, final)
        self.assertTrue(stats["available"])
        self.assertTrue(stats["summary_changed"])
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["unchanged"], 2)

    def test_action_match(self):
        expected = ["评估异步导出的队列与限流方案", "前端导出任务列表联调"]
        actual = ["评估异步导出的队列与限流方案", "完全无关"]
        result = action_match(expected, actual)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["recall"], 0.5)

    def test_text_similarity(self):
        self.assertGreater(text_similarity("导出改异步", "导出改成异步"), 0.5)
        self.assertEqual(text_similarity("", ""), 1.0)


if __name__ == "__main__":
    unittest.main()
