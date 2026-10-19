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


class ProductSwitchesTest(unittest.TestCase):
    def test_pg_index_provenance_is_captured(self):
        payload = {"data": {"model": "qwen", "pg_kb_table": "kb.chunk_search",
                            "vector_dim": 1024}}
        with mock.patch.object(rag, "_http_json", return_value=payload):
            switches = rag._product_switches("http://example", "token")
        self.assertEqual(switches["pg_kb_table"], "kb.chunk_search")
        self.assertEqual(switches["vector_dim"], 1024)


class HardNegativeSetTest(unittest.TestCase):
    def test_hard_negative_set_is_well_formed(self):
        path = EVALUATION_DIR / "gold" / "qa_seed_hard_negative.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["cases"]), 20)
        self.assertEqual(len({case["id"] for case in data["cases"]}), 20)
        for case in data["cases"]:
            self.assertTrue(case["unanswerable"], case["id"])
            self.assertEqual(case["expected_facts"], [], case["id"])
            self.assertEqual(case["case_source"], "agent_draft", case["id"])
            self.assertIn("硬负样", case["note"], case["id"])


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

    def test_meta_claims_are_excluded_from_the_content_caliber(self):
        """出处说明类元陈述不进内容级口径：首测 81 条"不支撑"里 67 条是这一类。"""
        packet = self._packet({
            "context": {"contexts": [], "facts": []},
            "answer": {"claims": [
                {"index": 1, "supported": True, "meta": False},
                {"index": 2, "supported": False, "meta": False},
                {"index": 3, "supported": False, "meta": True},
                {"index": 4, "supported": False, "meta": True},
            ], "answered": True},
        })
        summary = rag.summarize(packet)
        self.assertEqual(summary["faithfulness"]["rate"], 0.25, "全部口径照旧")
        self.assertEqual(summary["faithfulness_content"]["rate"], 0.5)
        self.assertEqual(summary["faithfulness_content"]["total"], 2)
        self.assertEqual(summary["faithfulness_meta_claims"], 2)
        self.assertEqual(summary["per_case"][0]["meta_claims"], 2)

    def test_content_caliber_is_null_when_the_judge_never_marked_meta(self):
        """旧提示词的标签没有 meta 字段 → 内容级算不出来，报 null 而不是等于全部。"""
        packet = self._packet({
            "context": {"contexts": [], "facts": []},
            "answer": {"claims": [{"index": 1, "supported": True}], "answered": True},
        })
        summary = rag.summarize(packet)
        self.assertEqual(summary["faithfulness"]["rate"], 1.0)
        self.assertIsNone(summary["faithfulness_content"])
        self.assertIsNone(summary["faithfulness_meta_claims"])
        self.assertIsNone(summary["per_case"][0]["faithfulness_content"])

    def test_case_whose_claims_are_all_meta_has_no_content_caliber(self):
        packet = self._packet({
            "context": {"contexts": [], "facts": []},
            "answer": {"claims": [{"index": 1, "supported": False, "meta": True}],
                       "answered": True},
        })
        summary = rag.summarize(packet)
        self.assertEqual(summary["faithfulness"]["rate"], 0.0)
        self.assertIsNone(summary["faithfulness_content"], "内容级没有样本时报 null")

    def test_answered_flag_counts_meta_claims_normally(self):
        """口径修正只动 faithfulness：其它三个指标的分母一个都没变。"""
        packet = self._packet({
            "context": {"contexts": [{"index": 1, "useful": True}],
                        "facts": [{"index": 1, "found": True}]},
            "answer": {"claims": [{"index": 1, "supported": True, "meta": True}],
                       "answered": True},
        })
        summary = rag.summarize(packet)
        self.assertEqual(summary["context_precision"]["rate"], 1.0)
        self.assertEqual(summary["context_recall"]["rate"], 1.0)
        self.assertEqual(summary["answer_relevancy"]["rate"], 1.0)

    def test_unanswerable_cases_are_reported_separately(self):
        """负样不能并进四个指标：它们没有正确答案，混进去会把"拒答"读成"检索无用"。"""
        packet = {"case_count": 2, "cases": [
            {"case_id": "q1", "question": "问", "expected_facts": ["甲"],
             "answer": "答", "contexts": [], "has_answer": True,
             "judgement": {"context": {"contexts": [{"index": 1, "useful": True}],
                                       "facts": [{"index": 1, "found": True}]},
                           "answer": {"claims": [{"index": 1, "supported": True}],
                                      "answered": True}}},
            {"case_id": "n1", "question": "会上定了多少预算？", "expected_facts": [],
             "unanswerable": True, "answer": "资料里没有提到预算。", "contexts": [],
             "has_answer": True,
             "judgement": {"context": {"contexts": [{"index": 1, "useful": False},
                                                   {"index": 2, "useful": False}], "facts": []},
                           "answer": {"claims": [{"index": 1, "supported": False}],
                                      "refused": True, "fabricated": False}}},
        ]}
        summary = rag.summarize(packet)
        self.assertEqual(summary["context_precision"]["total"], 1, "负样不进主分母")
        self.assertEqual(summary["context_recall"]["total"], 1)
        self.assertEqual(summary["answer_relevancy"]["total"], 1)
        negative = summary["unanswerable"]
        self.assertEqual(negative["cases"], 1)
        self.assertEqual(negative["cases_judged"], 1)
        self.assertEqual(negative["refusal_rate"]["rate"], 1.0)
        self.assertEqual(negative["forced_answer_rate"]["rate"], 0.0)
        self.assertEqual(negative["fabricated_rate"]["rate"], 0.0)
        self.assertEqual(negative["context_useful_rate"]["rate"], 0.0)
        self.assertEqual(negative["faithfulness"]["rate"], 0.0)

    def test_generic_answered_is_not_a_refusal_detector(self):
        """通用 answered 判的是"有没有回应"，不是"有没有拒答"：实测 12 条负样全中招。"""
        packet = {"case_count": 1, "cases": [
            {"case_id": "n1", "question": "问", "expected_facts": [],
             "unanswerable": True, "answer": "资料不足，没有找到。", "contexts": [],
             "has_answer": True,
             "judgement": {"context": {"contexts": [], "facts": []},
                           "answer": {"claims": [], "answered": False}}},
        ]}
        summary = rag.summarize(packet)
        negative = summary["unanswerable"]
        self.assertIsNone(negative["refusal_rate"], "没有 refused 字段时报判不了，不报 0%")
        self.assertIsNone(negative["fabricated_rate"])
        self.assertIn("缺 refused", negative["instrument"])

    def test_unanswerable_without_judgement_is_counted_not_scored(self):
        packet = {"case_count": 1, "cases": [
            {"case_id": "n1", "question": "问", "expected_facts": [],
             "unanswerable": True, "answer": "", "contexts": [], "has_answer": False},
        ]}
        summary = rag.summarize(packet)
        self.assertEqual(summary["unanswerable"]["cases"], 1)
        self.assertEqual(summary["unanswerable"]["cases_judged"], 0)
        self.assertIsNone(summary["unanswerable"]["refusal_rate"])

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


class JudgeShapeTest(unittest.TestCase):
    """判定响应的形状校验：截断的响应必须算**判定失败**，不能静默缩小分母。

    实测事故（2026-10-16）：判定模型在 JSON 模式下遇到带引号的句子会中途停住并补上
    闭合括号，返回的对象 JSON 可解析但内容被截断——最后一条断言少了 supported、
    answered 整字段消失。不查的话断言总数从 495 掉到 125，报告上看不出任何异常。
    """

    def test_a_complete_answer_response_passes(self):
        self.assertEqual(rag._answer_shape_problem(
            {"claims": [{"index": 1, "text": "x", "supported": True, "meta": False}],
             "answered": True, "why": "一句话"}), "")

    def test_empty_claims_are_legal(self):
        self.assertEqual(rag._answer_shape_problem({"claims": [], "answered": False}), "")

    def test_missing_answered_is_a_problem(self):
        self.assertIn("answered", rag._answer_shape_problem({"claims": []}))

    def test_negative_shape_requires_refused_and_fabricated(self):
        self.assertEqual(rag._negative_answer_shape_problem(
            {"refused": True, "fabricated": False, "why": "没有找到"}), "")
        self.assertIn("refused", rag._negative_answer_shape_problem(
            {"fabricated": False}))
        self.assertIn("fabricated", rag._negative_answer_shape_problem(
            {"refused": True}))
        self.assertIn("布尔", rag._negative_answer_shape_problem(
            {"refused": "yes", "fabricated": False}))

    def test_truncated_claim_is_a_problem(self):
        problem = rag._answer_shape_problem(
            {"claims": [{"index": 1, "text": "以上信息均来自"}], "answered": True})
        self.assertIn("supported", problem)

    def test_context_shape_checks_both_lists(self):
        self.assertEqual(rag._context_shape_problem(
            {"contexts": [{"index": 1, "useful": True}], "facts": [{"index": 1, "found": True}]}), "")
        self.assertIn("useful", rag._context_shape_problem({"contexts": [{"index": 1}]}))
        self.assertIn("found", rag._context_shape_problem({"facts": [{"index": 1}]}))

    def test_a_truncated_judgement_is_retried_once_then_recorded(self):
        """截断的响应先重试一次（附 re-ask 说明），两次都不行才记失败。

        "记失败"是必须的：样本不进分母，且 `_needs_judging` 会认它，下次运行补判。
        """
        packet = rag.build_packet(_qa_set(), _answers())
        replies = [{"parsed": {"contexts": [{"index": 1, "useful": True}], "facts": []}},
                   {"parsed": {"claims": []}},          # answer 第 1 次：缺 answered
                   {"parsed": {"claims": []}}]          # 重试后仍然缺
        with mock.patch.object(rag.model_client, "endpoint", return_value=("http://x", "m", "k")), \
                mock.patch.object(rag.model_client, "call_json",
                                  side_effect=replies) as call_json, \
                mock.patch.object(rag.time, "sleep", return_value=None), \
                tempfile.TemporaryDirectory() as folder:
            result = rag.judge_packet(packet, output_path=Path(folder) / "p.json",
                                      authorized_calls=3, progress=lambda *_: None)
        judgement = packet["cases"][0]["judgement"]
        self.assertIn("context", judgement)
        self.assertNotIn("answer", judgement)
        self.assertIn("形状不合格", judgement["answer_error"])
        self.assertEqual(result["calls_made"], 3, "重试的调用也要记账")
        self.assertIn("上一次输出不合格", call_json.call_args_list[-1].args[4],
                      "重试必须带上 re-ask 说明")
        self.assertTrue(rag._needs_judging(packet["cases"][0], "a"), "有失败的一半就必须能重判")
        summary = rag.summarize(packet)
        self.assertIsNone(summary["answer_relevancy"], "判不了的不进分母，也不填 0")

    def test_a_truncated_judgement_recovers_on_the_retry(self):
        packet = rag.build_packet(_qa_set(), _answers())
        replies = [{"parsed": {"contexts": [{"index": 1, "useful": True}], "facts": []}},
                   {"parsed": {"claims": [{"index": 1, "text": "x"}]}},   # 截断
                   {"parsed": {"claims": [{"index": 1, "text": "x", "supported": True}],
                               "answered": True}}]                        # 重试成功
        with mock.patch.object(rag.model_client, "endpoint", return_value=("http://x", "m", "k")), \
                mock.patch.object(rag.model_client, "call_json", side_effect=replies), \
                mock.patch.object(rag.time, "sleep", return_value=None), \
                tempfile.TemporaryDirectory() as folder:
            rag.judge_packet(packet, output_path=Path(folder) / "p.json",
                             authorized_calls=3, progress=lambda *_: None)
        self.assertEqual(packet["cases"][0]["judgement"]["answer"]["answered"], True)

    def test_the_retry_never_breaks_the_authorized_budget(self):
        """授权 2 次时，重试没有额度可用 → 记"额度用完"而不是多打一次调用。"""
        packet = rag.build_packet(_qa_set(), _answers())
        replies = [{"parsed": {"contexts": [{"index": 1, "useful": True}], "facts": []}},
                   {"parsed": {"claims": []}}]
        with mock.patch.object(rag.model_client, "endpoint", return_value=("http://x", "m", "k")), \
                mock.patch.object(rag.model_client, "call_json",
                                  side_effect=replies) as call_json, \
                mock.patch.object(rag.time, "sleep", return_value=None), \
                tempfile.TemporaryDirectory() as folder:
            result = rag.judge_packet(packet, output_path=Path(folder) / "p.json",
                                      authorized_calls=2, progress=lambda *_: None)
        self.assertEqual(result["calls_made"], 2)
        self.assertEqual(call_json.call_count, 2)
        self.assertIn("调用已用完", packet["cases"][0]["judgement"]["answer_error"])


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


class CaptureRefsTest(unittest.TestCase):
    def test_refs_outside_the_message_snapshot_are_counted(self):
        """扩展集 q93 的死链：答案写 [1][3][5]，快照只有 [1]。"""
        message = {"content": "结论 [1]，下午场 [3][5]。", "citations": [{"ref": 1}]}
        self.assertEqual(rag._unknown_refs(message), [3, 5])

    def test_all_refs_in_snapshot_is_clean(self):
        message = {"content": "结论 [1][2]。", "citations": [{"ref": 1}, {"ref": 2}]}
        self.assertEqual(rag._unknown_refs(message), [])


class ChatChecksTest(unittest.TestCase):
    """两条零调用不变量：定位与越权。判据来自产品契约层（登记在案的例外②）。

    对照物是**会议的转写全文**（产物里的 `source_texts`），不是检索结果本身——
    引文本来就是从检索结果拷的，拿它比对等于自己跟自己比。
    """

    SOURCES = {"2": {"meeting_id": 2, "segment_count": 2,
                     "text": "张晨本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户\n"
                             "李月灰度期间登录成功率 99.8%"}}

    def _turn(self, **overrides) -> dict:
        turn = {
            "message_id": 1, "question": "问",
            "allowed_meeting_ids": [2],
            "used_refs": [1],
            "unknown_refs": [],
            "citations": [{
                "ref": 1, "kind": "chunk", "meeting_id": 2,
                # 切片渲染时会加定位前缀，判定位前先剥掉
                "quote": "[51s][张晨] 本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
            }],
        }
        turn.update(overrides)
        return turn

    def _summarize(self, turns):
        return chat_checks.summarize(turns, sources=self.SOURCES)

    def test_clean_turn_passes_both_invariants(self):
        summary = self._summarize([self._turn()])
        self.assertEqual(summary["locatable"]["rate"], 1.0)
        self.assertEqual(summary["authorized"]["rate"], 1.0)
        self.assertEqual(summary["turns_with_violation"], 0)

    def test_unlocatable_quote_is_caught(self):
        turn = self._turn(citations=[{
            "ref": 1, "kind": "chunk", "meeting_id": 2,
            "quote": "本次灰度从 7 月 1 日开始",  # 这句话在转写里不存在（模型编的）
        }])
        summary = self._summarize([turn])
        self.assertEqual(summary["locatable"]["rate"], 0.0)
        self.assertEqual(summary["unlocatable_count"], 1)
        self.assertEqual(summary["turns_with_violation"], 1)

    def test_citation_outside_the_visible_scope_is_caught(self):
        turn = self._turn(citations=[{
            "ref": 1, "kind": "chunk", "meeting_id": 99,   # 不在可见范围内
            "quote": "[51s][张晨] 本次灰度从 8 月 20 日开始覆盖 10% 的活跃用户",
        }])
        summary = self._summarize([turn])
        self.assertEqual(summary["authorized"]["rate"], 0.0)
        self.assertEqual(summary["out_of_scope_count"], 1)

    def test_admin_scope_none_means_no_restriction(self):
        summary = self._summarize([self._turn(allowed_meeting_ids=None)])
        self.assertEqual(summary["authorized"]["rate"], 1.0)

    def test_inventory_fact_is_not_applicable_not_unlocatable(self):
        """录音/会议清单是元数据卡片，不是逐字转写；不能拿转写定位它。"""
        turn = self._turn(
            used_refs=[1, 2],
            citations=[
                self._turn()["citations"][0],
                {"ref": 2, "kind": "fact", "meeting_id": 2,
                 "quote": "录音 task 7 · 2026-09-10 14:00:00 · 下午",
                 "task_id": 7, "source_id": 7},
            ])
        summary = self._summarize([turn])
        self.assertEqual(summary["locatable"]["rate"], 1.0)
        self.assertEqual(summary["unlocatable_count"], 0)
        self.assertEqual(summary["not_applicable_count"], 1)
        self.assertEqual(summary["turns_with_violation"], 0)

    def test_citation_without_a_transcript_reference_is_reported_as_unchecked(self):
        """会议转写没抓到（例如来源不是转写）时判不了定位：如实计数，不算通过。"""
        turn = self._turn(citations=[{"ref": 1, "kind": "chunk", "meeting_id": 999, "quote": "x"}])
        summary = self._summarize([turn])
        self.assertIsNone(summary["locatable"], "判不了的不算通过，也不进分母")
        self.assertEqual(summary["unchecked_citation_count"], 1)

    def test_used_ref_missing_from_the_registry_is_counted(self):
        turn = self._turn(used_refs=[1, 7], unknown_refs=[7])
        summary = self._summarize([turn])
        self.assertEqual(summary["missing_ref_count"], 1)
        self.assertEqual(summary["unknown_ref_count"], 1)

    def test_locator_prefixes_are_stripped_before_locating(self):
        self.assertEqual(
            chat_checks.strip_locators("[51s][张晨] 第一句\n[74s][李月] 第二句"),
            "第一句\n第二句")
        # 普通方括号内容不受影响（只剥行首的定位前缀序列）
        self.assertEqual(chat_checks.strip_locators("见 [附录] 第三条"), "见 [附录] 第三条")

    def test_empty_sample_reports_no_sample_not_zero(self):
        summary = self._summarize([])
        self.assertEqual(summary["turn_count"], 0)
        self.assertIsNone(summary.get("locatable"))
        self.assertIn("无样本", summary["reading"])

    def test_demo_artifact_passes(self):
        artifact = chat_checks.load_artifact(chat_checks.DEMO_ARTIFACT)
        self.assertIsNotNone(artifact, "演示样本必须随仓库提供")
        summary = chat_checks.summarize(artifact["turns"], sources=artifact.get("source_texts"))
        self.assertEqual(summary["turns_with_violation"], 0)
        self.assertEqual(summary["locatable"]["rate"], 1.0)
        self.assertEqual(summary["out_of_scope_count"], 0)

    def test_qa_set_is_well_formed(self):
        qa_set = json.loads((EVALUATION_DIR / "gold" / "qa_seed.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(qa_set["cases"]), 100,
                                "意图 F1 需要 100+ 条（27 条只够看方向）")
        taxonomy = qa_set["annotation"]["intent_taxonomy"]
        ids = [case["id"] for case in qa_set["cases"]]
        self.assertEqual(len(ids), len(set(ids)), "id 不能重复")
        for case in qa_set["cases"]:
            self.assertTrue(case["question"].strip())
            if case.get("unanswerable"):
                # 负样：没有正确答案，也没有意图标签；只要求金标为空且可追溯
                self.assertEqual(case.get("expected_facts"), [], case["id"])
                self.assertNotIn("source", case, case["id"])
                continue
            self.assertIn(case["intent"], taxonomy, case["id"])
            # 标注来源要如实分层：代理起草的不许冒充人工标注
            self.assertIn(case["intent_source"], ("agent_draft", "human"), case["id"])
            if case["intent"] == "smalltalk":
                # 寒暄轮没有会议金标——它们考的是"不该去检索、也不该编内容"
                self.assertFalse(case.get("expected_facts"), case["id"])
                self.assertNotIn("source", case, case["id"])
                continue
            self.assertTrue(case["expected_facts"], case["id"])
            self.assertIn(case["source"]["task_id"], (6, 7), case["id"])
            self.assertTrue(case["source"]["segments"], case["id"])
        # 宪法第 3 条：小样本只作方向信号，这一点必须写在数据里
        self.assertIn("方向信号", qa_set["discipline"])

    def test_extended_set_is_a_separate_heldout_sample(self):
        """68 条扩展集与 27 条人工集分开报：全部代理起草、有金标片段、未参与调参。"""
        qa_set = json.loads((EVALUATION_DIR / "gold" / "qa_seed_extended.json")
                            .read_text(encoding="utf-8"))
        cases = qa_set["cases"]
        self.assertEqual(len(cases), 68)
        for case in cases:
            self.assertEqual(case.get("case_source"), "agent_draft", case["id"])
            self.assertTrue(case.get("expected_facts"), case["id"])
            self.assertTrue(case.get("source", {}).get("segments"), case["id"])
            self.assertIn(case.get("intent"), ("single_fact", "aggregation", "cross_meeting"))
        self.assertIn("未参与本轮", qa_set["annotation"]["holdout_note"])
        self.assertIn("代理起草", qa_set["discipline"])


if __name__ == "__main__":
    unittest.main()


class IntentAnnotationTest(unittest.TestCase):
    """意图标注包：盲判（代理标签不进包）+ 判据齐备。"""

    def test_packet_is_blind_and_covers_every_case(self):
        from gold import annotation
        packet = annotation.build_intent_packet()
        qa_set = json.loads((EVALUATION_DIR / "gold" / "qa_seed.json").read_text(encoding="utf-8"))
        answerable = [case for case in qa_set["cases"] if not case.get("unanswerable")]
        self.assertEqual(len(packet["items"]), len(answerable))
        for item in packet["items"]:
            self.assertTrue(item["question"].strip())
            self.assertEqual(set(item["labels"]), set(packet["labels_allowed"]))
            self.assertTrue(all(value is None for value in item["labels"].values()))
            # 代理标签不许进包（会锚定人工标注）：条目里既没有意图值也没有它的来源
            self.assertNotIn("intent", item)
            self.assertNotIn("intent_source", item)
        # 每一类都要有判据（标注者按表判，不按感觉判）
        self.assertEqual(len(packet["criteria"]), len(packet["labels_allowed"]))
        for criterion in packet["criteria"]:
            self.assertTrue(criterion["meaning"] and criterion["criterion"])
