"""轴 5（检索与回答）与问答引用零调用检查的单测：零模型、零网络。

三组断言各自对应一条容易静默出错的性质：
- **组包**：抓到的答案能按问题配上标注（配不上时 `has_answer=False`，不是抛异常）；
- **汇总**：四个指标各自独立、判不了的样本单独计数（不混进分母、不报 0 分）；
- **预算闸**：`--judge` 没传 `--authorized-llm-calls` 必须拒绝，且调用数按
  "每 case 两次"折算——写错折算会超支。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from judges import rag  # noqa: E402
from regression import chat_checks  # noqa: E402


def _qa_set() -> dict:
    return {
        "name": "测试集",
        "discipline": "n 小，只作方向信号",
        "cases": [
            {"id": "q1", "question": "灰度从什么时候开始？",
             "expected_facts": ["8 月 20 日开始", "覆盖 10% 活跃用户"]},
            {"id": "q2", "question": "回滚要多久？", "expected_facts": ["最快五分钟"]},
        ],
    }


def _answers() -> dict:
    return {
        "artifact_version": 1,
        "turns": [
            {"question": "灰度从什么时候开始？",
             "answer": "灰度从 8 月 20 日开始，覆盖 10% 的活跃用户[1]。",
             "citations": [{"ref": 1, "kind": "chunk", "meeting_id": 2,
                            "quote": "本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
                            "source_text": "本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户"}]},
        ],
    }


class BuildPacketTest(unittest.TestCase):
    def test_answers_are_matched_by_question(self):
        packet = rag.build_packet(_qa_set(), _answers())
        by_id = {case["case_id"]: case for case in packet["cases"]}
        self.assertTrue(by_id["q1"]["has_answer"])
        self.assertEqual(len(by_id["q1"]["contexts"]), 1)
        # 没抓到答案的问题留在包里，标记 has_answer=False（不静默丢样本）
        self.assertFalse(by_id["q2"]["has_answer"])
        self.assertEqual(packet["answered_case_count"], 1)

    def test_build_without_answers_is_still_valid(self):
        packet = rag.build_packet(_qa_set(), None)
        self.assertEqual(packet["answered_case_count"], 0)
        self.assertEqual(len(packet["cases"]), 2)


class SummarizeTest(unittest.TestCase):
    def _packet(self, judgement: dict, case: dict | None = None) -> dict:
        base = case or {"case_id": "q1", "question": "问", "expected_facts": ["甲", "乙"],
                        "answer": "答", "contexts": [], "has_answer": True}
        base = dict(base)
        base["judgement"] = judgement
        return {"case_count": 1, "cases": [base]}

    def test_four_metrics_are_reported_separately(self):
        packet = self._packet({
            "context": {"contexts": [{"index": 1, "useful": True}, {"index": 2, "useful": False}],
                        "facts": [{"index": 1, "found": True}, {"index": 2, "found": False}]},
            "answer": {"claims": [{"index": 1, "supported": True},
                                  {"index": 2, "supported": True},
                                  {"index": 3, "supported": False}],
                       "answered": True},
        })
        summary = rag.summarize(packet)
        self.assertEqual(summary["context_precision"]["rate"], 0.5)
        self.assertEqual(summary["context_recall"]["rate"], 0.5)
        self.assertAlmostEqual(summary["faithfulness"]["rate"], 2 / 3)
        self.assertEqual(summary["answer_relevancy"]["rate"], 1.0)
        # 没有任何加权总分的字段
        self.assertNotIn("overall", summary)
        self.assertNotIn("score", summary)

    def test_cases_without_gold_are_counted_not_scored(self):
        packet = self._packet({
            "context": {"contexts": [{"index": 1, "useful": True}], "facts": []},
            "answer": {"claims": [{"index": 1, "supported": True}], "answered": False},
        }, case={"case_id": "q9", "question": "问", "expected_facts": [], "answer": "答",
                 "contexts": [], "has_answer": True})
        summary = rag.summarize(packet)
        self.assertIsNone(summary["context_recall"], "无金标时必须报检不出，不能填 0")
        self.assertEqual(summary["cases_step_without_gold"], 1)
        # 有金标的样本数为 0 时区间也不该给出来
        packet2 = self._packet({"context": {"contexts": [], "facts": []},
                                "answer": {"claims": [], "answered": True}})
        summary2 = rag.summarize(packet2)
        self.assertIsNone(summary2["context_precision"])
        self.assertIsNone(summary2["faithfulness"])

    def test_unjudged_cases_are_not_in_the_denominator(self):
        packet = {"case_count": 2, "cases": [
            {"case_id": "q1", "question": "问", "expected_facts": [], "answer": "答",
             "contexts": [], "has_answer": True,
             "judgement": {"context": {"contexts": [{"index": 1, "useful": True}], "facts": []},
                           "answer": {"claims": [], "answered": True}}},
            {"case_id": "q2", "question": "问2", "expected_facts": [], "answer": "答2",
             "contexts": [], "has_answer": True},
        ]}
        summary = rag.summarize(packet)
        self.assertEqual(summary["cases_judged"], 1)
        self.assertEqual(summary["context_precision"]["total"], 1)


class BudgetGateTest(unittest.TestCase):
    def test_judge_requires_an_explicit_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            packet_path = Path(folder) / "packet.json"
            packet_path.write_text(json.dumps(rag.build_packet(_qa_set(), _answers())),
                                   encoding="utf-8")
            code = rag.main(["--judge", "--packet", str(packet_path)])
        self.assertEqual(code, 2, "没传预算闸必须拒绝执行")

    def test_ask_requires_a_budget_and_a_token(self):
        code = rag.main(["--ask", "--token", "t"])
        self.assertEqual(code, 2)
        code = rag.main(["--ask", "--authorized-llm-calls", "5"])
        self.assertEqual(code, 2)

    def test_budget_is_spent_two_calls_per_case(self):
        packet = rag.build_packet(_qa_set(), _answers())
        with mock.patch.object(rag.model_client, "endpoint", return_value=("http://x", "m", "k")), \
                mock.patch.object(rag.model_client, "call_json",
                                  return_value={"parsed": {"answered": True}}) as call_json, \
                mock.patch.object(rag.time, "sleep", return_value=None), tempfile.TemporaryDirectory() as folder:
            result = rag.judge_packet(packet, output_path=Path(folder) / "p.json",
                                      authorized_calls=2, progress=lambda *_: None)
        # 授权 2 次 = 判 1 个 case（context + answer 两次调用）
        self.assertEqual(result["calls_made"], 2)
        self.assertEqual(result["judged"], 1)
        self.assertEqual(call_json.call_count, 2)

    def test_judge_failure_does_not_abort_the_batch(self):
        packet = rag.build_packet(_qa_set(), _answers())
        with mock.patch.object(rag.model_client, "endpoint", return_value=("http://x", "m", "k")), \
                mock.patch.object(rag.model_client, "call_json",
                                  side_effect=rag.model_client.ModelClientError("超时")), \
                mock.patch.object(rag.time, "sleep", return_value=None), tempfile.TemporaryDirectory() as folder:
            result = rag.judge_packet(packet, output_path=Path(folder) / "p.json",
                                      authorized_calls=2, progress=lambda *_: None)
        self.assertEqual(result["calls_made"], 2)
        case = packet["cases"][0]
        self.assertIn("context_error", case["judgement"])


class ChatChecksTest(unittest.TestCase):
    """两条零调用不变量：定位与越权。判据来自产品契约层（登记在案的例外②）。"""

    def _turn(self, **overrides) -> dict:
        turn = {
            "message_id": 1, "question": "问",
            "allowed_meeting_ids": [2],
            "used_refs": [1],
            "unknown_refs": [],
            "citations": [{
                "ref": 1, "kind": "chunk", "meeting_id": 2,
                "quote": "本次灰度从 8 月 20 日开始",
                "source_text": "张晨本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
            }],
        }
        turn.update(overrides)
        return turn

    def test_clean_turn_passes_both_invariants(self):
        summary = chat_checks.summarize([self._turn()])
        self.assertEqual(summary["locatable"]["rate"], 1.0)
        self.assertEqual(summary["authorized"]["rate"], 1.0)
        self.assertEqual(summary["turns_with_violation"], 0)

    def test_unlocatable_quote_is_caught(self):
        turn = self._turn(citations=[{
            "ref": 1, "kind": "chunk", "meeting_id": 2,
            "quote": "本次灰度从 7 月 1 日开始",  # 引文在切片里找不到
            "source_text": "张晨本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
        }])
        summary = chat_checks.summarize([turn])
        self.assertEqual(summary["locatable"]["rate"], 0.0)
        self.assertEqual(summary["unlocatable_count"], 1)
        self.assertEqual(summary["turns_with_violation"], 1)

    def test_citation_outside_the_visible_scope_is_caught(self):
        turn = self._turn(citations=[{
            "ref": 1, "kind": "chunk", "meeting_id": 99,   # 不在可见范围内
            "quote": "本次灰度从 8 月 20 日开始",
            "source_text": "张晨本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
        }])
        summary = chat_checks.summarize([turn])
        self.assertEqual(summary["authorized"]["rate"], 0.0)
        self.assertEqual(summary["out_of_scope_count"], 1)

    def test_admin_scope_none_means_no_restriction(self):
        summary = chat_checks.summarize([self._turn(allowed_meeting_ids=None)])
        self.assertEqual(summary["authorized"]["rate"], 1.0)

    def test_citation_without_source_text_is_reported_as_unchecked(self):
        turn = self._turn(citations=[{"ref": 1, "kind": "chunk", "meeting_id": 2, "quote": "x"}])
        summary = chat_checks.summarize([turn])
        self.assertIsNone(summary["locatable"], "判不了的不算通过，也不进分母")
        self.assertEqual(summary["unchecked_citation_count"], 1)

    def test_used_ref_missing_from_the_registry_is_counted(self):
        turn = self._turn(used_refs=[1, 7], unknown_refs=[7])
        summary = chat_checks.summarize([turn])
        self.assertEqual(summary["missing_ref_count"], 1)
        self.assertEqual(summary["unknown_ref_count"], 1)

    def test_empty_sample_reports_no_sample_not_zero(self):
        summary = chat_checks.summarize([])
        self.assertEqual(summary["turn_count"], 0)
        self.assertIsNone(summary.get("locatable"))
        self.assertIn("无样本", summary["reading"])

    def test_demo_artifact_passes(self):
        artifact = chat_checks.load_artifact(chat_checks.DEMO_ARTIFACT)
        self.assertIsNotNone(artifact, "演示样本必须随仓库提供")
        summary = chat_checks.summarize(artifact["turns"])
        self.assertEqual(summary["turns_with_violation"], 0)
        self.assertEqual(summary["locatable"]["rate"], 1.0)
        self.assertEqual(summary["out_of_scope_count"], 0)

    def test_qa_set_is_well_formed(self):
        qa_set = json.loads((EVALUATION_DIR / "gold" / "qa_seed.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(qa_set["cases"]), 20, "标注集太小就称不上 ground truth")
        ids = [case["id"] for case in qa_set["cases"]]
        self.assertEqual(len(ids), len(set(ids)), "id 不能重复")
        for case in qa_set["cases"]:
            self.assertTrue(case["question"].strip())
            self.assertTrue(case["expected_facts"], case["id"])
            self.assertIn(case["source"]["task_id"], (6, 7), case["id"])
        # 宪法第 3 条：小样本只作方向信号，这一点必须写在数据里
        self.assertIn("方向信号", qa_set["discipline"])


if __name__ == "__main__":
    unittest.main()
