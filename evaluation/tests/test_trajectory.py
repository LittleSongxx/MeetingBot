"""轨迹评测单测：strict/acceptable、工具集合 P/R/F1、必引、禁止、pass^k、无样本路径。

固定小样本，全零模型调用。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from regression import trajectory  # noqa: E402

GOLD_PATH = EVALUATION_DIR / "gold" / "trajectory_27.json"
CROSS_PATH = EVALUATION_DIR / "gold" / "cross_meeting_qa_30.json"
ALL_TOOLS = ["search_knowledge", "list_facts", "get_minutes",
             "get_transcript_excerpt", "query_meeting_state"]


def _case(case_id: str, question: str, intent: str, **overrides) -> dict:
    case = {
        "case_id": case_id,
        "question": question,
        "intent": intent,
        "required_tools": [],
        "acceptable_tools": [],
        "forbidden_tools": [],
        "max_tool_calls": 6,
        "min_tool_calls": 0,
        "must_cite": True,
        "cross_meeting": False,
        "label_source": "agent_draft_heuristic",
        "rule": "单元测试用",
    }
    case.update(overrides)
    return case


def _gold() -> dict:
    return {"cases": [
        _case("a1", "上线时间是什么", "single_fact",
              required_tools=["search_knowledge"], acceptable_tools=["search_knowledge"],
              min_tool_calls=1, max_tool_calls=4),
        _case("a2", "有哪些待办", "aggregation",
              required_tools=["list_facts"],
              acceptable_tools=["list_facts", "search_knowledge"], min_tool_calls=1),
        _case("a3", "两段分别怎样", "cross_meeting",
              required_tools=["search_knowledge"],
              acceptable_tools=["search_knowledge", "list_facts"],
              min_tool_calls=2, max_tool_calls=8, cross_meeting=True),
        _case("a4", "你好", "smalltalk", required_tools=[], acceptable_tools=[],
              forbidden_tools=list(ALL_TOOLS), max_tool_calls=0, min_tool_calls=0,
              must_cite=False),
        _case("a5", "上次待办还有哪些", "state",
              required_tools=["query_meeting_state"],
              acceptable_tools=["query_meeting_state", "search_knowledge"],
              min_tool_calls=1),
    ]}


def _turn(question: str, tools: list[str] | None = None,
          citations: list[dict] | None = None) -> dict:
    return {
        "question": question,
        "answer": "测试答案",
        "stop_reason": "finished",
        "tools": [{"name": name, "ok": True} for name in (tools or [])],
        "citations": citations if citations is not None else [{"ref": 1, "quote": "原文"}],
    }


def _cite() -> list[dict]:
    return [{"ref": 1, "quote": "原文"}]


def _artifacts() -> list[dict]:
    """三次 trial：a1 全对；a2 首轮错后改对；a3 第二次少检索；a4 第二次乱调工具。"""
    trial1 = {"turns": [
        _turn("上线时间是什么", ["search_knowledge"], _cite()),
        _turn("有哪些待办", ["search_knowledge"], _cite()),
        _turn("两段分别怎样", ["search_knowledge", "search_knowledge"], _cite()),
        _turn("你好", [], []),
        _turn("上次待办还有哪些", ["search_knowledge"], _cite()),
    ]}
    trial2 = {"turns": [
        _turn("上线时间是什么", ["search_knowledge"], _cite()),
        _turn("有哪些待办", ["list_facts"], _cite()),
        _turn("两段分别怎样", ["search_knowledge"], _cite()),
        _turn("你好", ["search_knowledge"], _cite()),
        _turn("上次待办还有哪些", ["query_meeting_state"], _cite()),
    ]}
    trial3 = {"turns": [
        _turn("上线时间是什么", ["search_knowledge"], _cite()),
        # 超过 max_tool_calls=6 的预算调用：pass 不因预算改判，只进 tool_budget
        _turn("有哪些待办", ["list_facts"] + ["search_knowledge"] * 6, _cite()),
        _turn("两段分别怎样", ["search_knowledge", "list_facts"], _cite()),
        _turn("你好", [], []),
        _turn("上次待办还有哪些", ["query_meeting_state"], _cite()),
    ]}
    return [trial1, trial2, trial3]


class MergeTrialsTest(unittest.TestCase):
    def test_trials_keep_artifact_order(self):
        trial1 = {"turns": [_turn("上线时间是什么", ["search_knowledge"])]}
        trial2 = {"turns": [_turn("上线时间是什么", ["list_facts"])]}
        grouped = trajectory.merge_trials([trial1, trial2])
        names = [trajectory.tool_names(turn) for turn in grouped["上线时间是什么"]]
        self.assertEqual(names, [["search_knowledge"], ["list_facts"]])


class EvaluateTrialTest(unittest.TestCase):
    def test_first_tool_strict_and_acceptable_are_separate(self):
        case = _gold()["cases"][1]      # aggregation：strict 要求 list_facts
        result = trajectory.evaluate_trial(case, _turn("有哪些待办", ["search_knowledge"]))
        self.assertFalse(result["strict"], "首轮用 search 起步不算 strict")
        self.assertTrue(result["acceptable"], "aggregation 允许 search 起步")

    def test_set_precision_recall_f1_dedupes_tools(self):
        case = _gold()["cases"][0]      # single_fact，required={search}
        result = trajectory.evaluate_trial(
            case, _turn("上线时间是什么", ["search_knowledge", "list_facts",
                                           "search_knowledge"]))
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(result["used_tools"], ["list_facts", "search_knowledge"])
        self.assertAlmostEqual(result["precision"], 0.5)
        self.assertAlmostEqual(result["recall"], 1.0)
        self.assertAlmostEqual(result["f1"], 2 / 3)
        self.assertTrue(result["required_ok"])

    def test_smalltalk_requires_no_tools_and_no_citation(self):
        case = _gold()["cases"][3]
        ok = trajectory.evaluate_trial(case, _turn("你好", [], []))
        self.assertTrue(ok["strict"])
        self.assertTrue(ok["acceptable"])
        self.assertTrue(ok["pass"])
        bad = trajectory.evaluate_trial(case, _turn("你好", ["search_knowledge"], _cite()))
        self.assertFalse(bad["required_ok"])
        self.assertFalse(bad["pass"])
        self.assertEqual(bad["forbidden_hits"], ["search_knowledge"])

    def test_min_tool_calls_gate(self):
        case = _gold()["cases"][2]      # cross_meeting 至少 2 次调用
        one = trajectory.evaluate_trial(case, _turn("两段分别怎样", ["search_knowledge"]))
        self.assertFalse(one["required_ok"])
        self.assertFalse(one["min_calls_ok"])
        two = trajectory.evaluate_trial(
            case, _turn("两段分别怎样", ["search_knowledge", "search_knowledge"]))
        self.assertTrue(two["required_ok"])

    def test_missing_citation_fails_must_cite(self):
        case = _gold()["cases"][0]
        result = trajectory.evaluate_trial(case, _turn("上线时间是什么",
                                                       ["search_knowledge"], []))
        self.assertTrue(result["required_ok"])
        self.assertFalse(result["citation_ok"])
        self.assertFalse(result["pass"])

    def test_budget_violation_is_reported_not_failed(self):
        case = _gold()["cases"][1]      # max_tool_calls=6
        result = trajectory.evaluate_trial(
            case, _turn("有哪些待办", ["list_facts"] + ["search_knowledge"] * 6))
        self.assertFalse(result["within_budget"])
        self.assertTrue(result["required_ok"] and result["pass"])


class SummarizeTest(unittest.TestCase):
    def test_rates_and_per_intent(self):
        summary = trajectory.summarize(_gold(), _artifacts(), k=2)
        self.assertEqual(summary["sample"], 5)
        self.assertEqual(summary["gold_cases"], 5)
        self.assertEqual(summary["first_tool"]["sample"], 5)
        # 首轮：a1 T, a2 F, a3 T, a4 T, a5 F
        self.assertAlmostEqual(summary["first_tool"]["strict"]["rate"], 3 / 5)
        self.assertEqual(summary["first_tool"]["strict"]["successes"], 3)
        # 首轮 acceptable：全中（a2 用 search、a5 用 search 都算可接受）
        self.assertAlmostEqual(summary["first_tool"]["acceptable"]["rate"], 1.0)
        self.assertEqual(summary["first_tool"]["per_intent"]["aggregation"]["support"], 1)
        # 必引只统计 must_cite 题：a1/a2/a3/a5 首 trial 都有引用
        self.assertEqual(summary["must_cite"]["sample"], 4)
        self.assertAlmostEqual(summary["must_cite"]["citations_present"]["rate"], 1.0)
        # 强制工具覆盖：a2 首轮缺 list_facts、a5 首轮缺 state → 3/5
        self.assertAlmostEqual(summary["required_coverage"]["covered"]["rate"], 3 / 5)
        # 禁止工具：a4 trial2 调了 search → 1 次命中
        self.assertEqual(summary["forbidden"]["hits"], 1)
        self.assertEqual(summary["forbidden"]["cases_with_hits"], 1)
        # 预算：a2 trial3 调 7 次 > 6；a4 trial2 调 1 次 > 0 → 两次超预算
        # （pass 不受影响：a2 仍 pass，a4 因 required_ok 失败）
        self.assertEqual(summary["tool_budget"]["trials_over_budget"], 2)
        self.assertEqual(summary["tool_budget"]["cases_over_budget"], 2)
        self.assertEqual({detail["case_id"] for detail in summary["tool_budget"]["details"]},
                         {"a2", "a4"})

    def test_tool_set_micro_and_macro(self):
        summary = trajectory.summarize(_gold(), _artifacts(), k=2)
        tool_set = summary["tool_set"]
        self.assertEqual(tool_set["sample"], 5)
        # 首 trial：a1 匹配 1；a2 0；a3 匹配 1；a4 required 空；a5 0
        self.assertEqual(tool_set["micro"]["matched_required"], 2)
        self.assertEqual(tool_set["micro"]["used_total"], 4)
        self.assertEqual(tool_set["micro"]["required_total"], 4)
        self.assertAlmostEqual(tool_set["micro"]["precision"], 0.5)
        self.assertAlmostEqual(tool_set["micro"]["recall"], 0.5)
        self.assertAlmostEqual(tool_set["macro"]["precision"], 0.6)
        self.assertAlmostEqual(tool_set["macro"]["recall"], 0.6)
        self.assertAlmostEqual(tool_set["macro"]["f1"], 0.6)
        # LEDGER 预注册的唯一主指标：逐题集合 F1 的平均（不合成其他分数）
        self.assertEqual(tool_set["primary_metric"], "tool_call_f1")
        self.assertAlmostEqual(tool_set["tool_call_f1"], 0.6)

    def test_pass_k_requires_all_k_trials(self):
        summary = trajectory.summarize(_gold(), _artifacts(), k=2)
        pass_k = summary["pass_k"]
        self.assertEqual(pass_k["evaluated_cases"], 5)
        # 只有 a1 两次 trial 都 pass；a2/a3/a4/a5 第一次都失败
        self.assertEqual(pass_k["passed"], 1)
        self.assertAlmostEqual(pass_k["rate"], 0.2)
        self.assertEqual(pass_k["insufficient_trials"], [])
        # 前 2 次 trial 共 10 次：trial1 pass 3（a1/a3/a4）+ trial2 pass 3（a1/a2/a5）
        self.assertEqual(pass_k["trial_level"]["trials"], 10)
        self.assertEqual(pass_k["trial_level"]["passed"], 6)
        self.assertTrue(pass_k["clopper_pearson_95"][0] <= 0.2 <= pass_k["clopper_pearson_95"][1])

    def test_trials_beyond_k_do_not_count_for_pass(self):
        summary = trajectory.summarize(_gold(), _artifacts(), k=3)
        self.assertEqual(summary["pass_k"]["evaluated_cases"], 5)
        # 只有 a1 三次 trial 全满足；a2/a3/a4/a5 都有至少一次失败
        self.assertEqual(summary["pass_k"]["passed"], 1)

    def test_insufficient_trials_are_listed_not_scored(self):
        summary = trajectory.summarize(_gold(), _artifacts()[:1], k=2)
        self.assertEqual(summary["sample"], 5)
        self.assertEqual(summary["pass_k"]["evaluated_cases"], 0)
        self.assertIsNone(summary["pass_k"]["rate"])
        self.assertEqual(len(summary["pass_k"]["insufficient_trials"]), 5)

    def test_missing_questions_are_listed(self):
        only_one = {"turns": [_turn("上线时间是什么", ["search_knowledge"], _cite())]}
        summary = trajectory.summarize(_gold(), [only_one], k=1)
        self.assertEqual(summary["sample"], 1)
        self.assertEqual(sorted(summary["missing"]), ["a2", "a3", "a4", "a5"])
        self.assertEqual(summary["pass_k"]["evaluated_cases"], 1)
        self.assertEqual(summary["pass_k"]["passed"], 1)

    def test_no_artifacts_reports_no_sample(self):
        summary = trajectory.summarize(_gold(), [], k=3)
        self.assertEqual(summary["sample"], 0)
        self.assertIn("无样本", summary["reading"])
        self.assertEqual(summary["gold_cases"], 5)

    def test_no_matching_question_reports_no_sample(self):
        artifact = {"turns": [_turn("完全无关的问题", ["search_knowledge"], _cite())]}
        summary = trajectory.summarize(_gold(), [artifact], k=1)
        self.assertEqual(summary["sample"], 0)
        self.assertIn("无样本", summary["reading"])


class GoldValidationTest(unittest.TestCase):
    def test_real_trajectory_gold_is_valid(self):
        gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        validation = trajectory.validate_gold(gold)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["cases"], 27)
        self.assertEqual(validation["by_intent"],
                         {"aggregation": 6, "cross_meeting": 5, "single_fact": 10,
                          "smalltalk": 2, "state": 4})

    def test_validate_catches_broken_rule(self):
        gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        broken = json.loads(json.dumps(gold))
        for case in broken["cases"]:
            if case["intent"] == "single_fact":
                case["required_tools"] = []
                case["acceptable_tools"] = []
                break
        validation = trajectory.validate_gold(broken)
        self.assertFalse(validation["ok"])
        self.assertTrue(any("single_fact" in error for error in validation["errors"]))

    def test_cross_meeting_gold_shape(self):
        cross = json.loads(CROSS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(cross["cases"]), 30)
        self.assertEqual(cross["counts"],
                         {"cross_meeting": 10, "series_state": 10, "temporal": 10})
        ids = set()
        for case in cross["cases"]:
            self.assertNotIn(case["case_id"], ids)
            ids.add(case["case_id"])
            self.assertIn(case["label_source"], {"agent_draft", "agent_draft_enriched", "agent_ai_reviewed", "independent_agent_reviewed"})
            self.assertFalse(case["human_checked"])
            if case["label_source"] != "agent_draft":
                self.assertTrue(case["expected_facts"], "补全/复核草稿每题必须有 expected_facts")
                self.assertIn("human_review", case["todo"])
            else:
                self.assertEqual(case["expected_facts"], [])
            self.assertIn(case["intent"], {"cross_meeting", "series_state", "temporal"})
            self.assertTrue(case.get("required_meetings") or case.get("required_series"),
                            "每题至少要有会议或系列范围")
            self.assertTrue(case.get("todo"), "expected_facts 未补时必须标 todo")


class CliTest(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = trajectory.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_no_artifact_exits_zero_and_reports_gold(self):
        code, payload = self._run(["--gold", str(GOLD_PATH)])
        self.assertEqual(code, 0)
        self.assertEqual(payload["sample"], 0)
        self.assertIn("无样本", payload["reading"])
        self.assertTrue(payload["gold_validation"]["ok"])
        self.assertTrue(payload["zero_model_calls"])

    def test_artifact_report_and_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold_path = root / "gold.json"
            gold_path.write_text(json.dumps(_gold(), ensure_ascii=False), encoding="utf-8")
            paths = []
            for index, artifact in enumerate(_artifacts()[:2]):
                path = root / f"trial{index}.json"
                path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
                paths.append(path)
            output = root / "report.json"
            argv = ["--gold", str(gold_path), "--k", "2", "--output", str(output)]
            for path in paths:
                argv += ["--artifact", str(path)]
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = trajectory.main(argv)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["artifact_count"], 2)
            self.assertEqual(payload["k"], 2)
            self.assertEqual(payload["sample"], 5)
            self.assertEqual(payload["pass_k"]["evaluated_cases"], 5)
            self.assertEqual(payload["pass_k"]["passed"], 1)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["sample"], payload["sample"])

    def test_bad_k_returns_two(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = trajectory.main(["--gold", str(GOLD_PATH), "--k", "0"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
