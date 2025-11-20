"""检索侧指标（recall@k / MRR / nDCG）的单测：零模型、零网络。

三组断言对应三条容易静默出错的性质：
- **金标片段与切片时间窗的映射**：切片是窗口、金标是句子，靠时间重叠判"覆盖"；
- **判不了要报无样本**：缺检索集或缺片段映射时不拿引用集冒充检索集（那会把召回算高）；
- **排序的位置价值**：相关切片排得越靠前，MRR 与 nDCG 越高。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from regression import retrieval_metrics as rm  # noqa: E402


def _artifact(retrieved, question="灰度从什么时候开始？"):
    return {
        "turns": [{"question": question, "retrieved": retrieved}],
        "source_texts": {"2": {"task_ids": [6], "segments": [
            {"task_id": 6, "segment_no": 18, "start_ms": 51_000, "end_ms": 55_000},
            {"task_id": 6, "segment_no": 19, "start_ms": 60_000, "end_ms": 66_000},
        ]}},
    }


def _qa(segments=(18,)):
    return {"cases": [{"id": "q01", "question": "灰度从什么时候开始？",
                       "source": {"task_id": 6, "segments": list(segments)}}]}


class SegmentMapTest(unittest.TestCase):
    def test_segments_map_to_start_seconds(self):
        starts = rm.segment_starts(_artifact([])["source_texts"], 6)
        self.assertEqual(starts, {18: 51, 19: 60})

    def test_other_tasks_are_excluded(self):
        self.assertEqual(rm.segment_starts(_artifact([])["source_texts"], 7), {})

    def test_old_artifacts_without_segments_yield_nothing(self):
        self.assertEqual(rm.segment_starts({"2": {"text": "全文"}}, 6), {})


class EvaluateTurnTest(unittest.TestCase):
    def _sources(self):
        return _artifact([])["source_texts"]

    def test_a_slice_covering_the_gold_segment_is_relevant(self):
        turn = {"retrieved": [{"start_second": 50, "end_second": 62}]}
        row = rm.evaluate_turn(turn, _qa()["cases"][0], self._sources())
        self.assertEqual(row["recall"], 1.0)
        self.assertEqual(row["mrr"], 1.0)

    def test_a_slice_outside_the_gold_window_does_not_count(self):
        turn = {"retrieved": [{"start_second": 100, "end_second": 120}]}
        row = rm.evaluate_turn(turn, _qa()["cases"][0], self._sources())
        self.assertEqual(row["recall"], 0.0)
        self.assertEqual(row["mrr"], 0.0)

    def test_rank_position_drives_mrr_and_ndcg(self):
        far = {"start_second": 100, "end_second": 120}
        near = {"start_second": 50, "end_second": 52}
        first = rm.evaluate_turn({"retrieved": [near, far]}, _qa()["cases"][0], self._sources())
        second = rm.evaluate_turn({"retrieved": [far, near]}, _qa()["cases"][0], self._sources())
        self.assertEqual(first["mrr"], 1.0)
        self.assertAlmostEqual(second["mrr"], 0.5)
        self.assertGreater(first["ndcg"], second["ndcg"])

    def test_slices_without_a_time_window_are_not_cover(self):
        """纪要条目切片没有时间窗：无从判定覆盖，**不算覆盖**也不假装算过。"""
        turn = {"retrieved": [{"start_second": None, "end_second": None}]}
        row = rm.evaluate_turn(turn, _qa()["cases"][0], self._sources())
        self.assertEqual(row["recall"], 0.0)

    def test_two_gold_segments_are_covered_partially(self):
        turn = {"retrieved": [{"start_second": 50, "end_second": 52}]}
        row = rm.evaluate_turn(turn, _qa(segments=(18, 19))["cases"][0], self._sources())
        self.assertEqual(row["recall"], 0.5)


class SourceAwareCoverageTest(unittest.TestCase):
    """两段录音的时间轴都从 0 开始：只比时间窗会把"上午 51 秒"与"下午 51 秒"算成同一条。

    切片带上来源身份（`source_id`）之后必须按来源判——否则召回会被假命中抬高。
    """

    SOURCES = {"2": {"task_ids": [6, 7], "segments": [
        {"task_id": 6, "segment_no": 18, "start_ms": 51_000, "end_ms": 55_000},
        {"task_id": 7, "segment_no": 1, "start_ms": 0, "end_ms": 5_000},
    ]}}
    CASE = {"id": "q01", "source": {"task_id": 6, "segments": [18]}}

    def test_same_window_from_another_recording_does_not_count(self):
        turn = {"retrieved": [{"start_second": 50, "end_second": 60, "source_id": 7}]}
        row = rm.evaluate_turn(turn, self.CASE, self.SOURCES)
        self.assertEqual(row["recall"], 0.0, "另一段录音的同一秒不是命中")

    def test_same_recording_counts(self):
        turn = {"retrieved": [{"start_second": 50, "end_second": 60, "source_id": 6}]}
        self.assertEqual(rm.evaluate_turn(turn, self.CASE, self.SOURCES)["recall"], 1.0)

    def test_old_artifacts_without_source_id_still_score_on_time_only(self):
        turn = {"retrieved": [{"start_second": 50, "end_second": 60}]}
        row = rm.evaluate_turn(turn, self.CASE, self.SOURCES)
        self.assertEqual(row["recall"], 1.0, "判不了来源时不拿它当否决条件")
        self.assertFalse(row["sources_known"])

    def test_cross_recording_gold_is_scorable(self):
        case = {"id": "q88", "source": {"task_id": 6, "segments": [18]},
                "also_in": {"task_id": 7, "segments": [1]}}
        turn = {"retrieved": [{"start_second": 50, "end_second": 60, "source_id": 6},
                              {"start_second": 0, "end_second": 3, "source_id": 7}]}
        row = rm.evaluate_turn(turn, case, self.SOURCES)
        self.assertEqual(row["recall"], 1.0, "跨录音题：两段都覆盖才算全")


class SummarizeTest(unittest.TestCase):
    def test_no_sample_instead_of_zero(self):
        summary = rm.summarize(_artifact([], question="别的问题"), _qa())
        self.assertEqual(summary["sample"], 0)
        self.assertIn("无样本", summary["reading"])
        self.assertTrue(summary["skipped"])

    def test_old_artifact_is_reported_as_unsampleable(self):
        artifact = _artifact([{"start_second": 50, "end_second": 62}])
        artifact["source_texts"] = {"2": {"text": "全文"}}   # 旧版抓取
        summary = rm.summarize(artifact, _qa())
        self.assertEqual(summary["sample"], 0)
        self.assertIn("片段", summary["skipped"][0]["reason"])

    def test_metrics_are_reported_separately(self):
        artifact = _artifact([{"start_second": 50, "end_second": 62}])
        summary = rm.summarize(artifact, _qa())
        self.assertEqual(summary["sample"], 1)
        self.assertEqual(summary["recall_at_k"], 1.0)
        self.assertEqual(summary["mrr"], 1.0)
        self.assertEqual(summary["ndcg_at_k"], 1.0)
        # 不合成总分
        self.assertNotIn("overall", summary)
        self.assertNotIn("score", summary)


if __name__ == "__main__":
    unittest.main()
