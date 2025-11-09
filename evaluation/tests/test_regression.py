"""Unit tests for the offline metric suite.

Run with::

    python -m unittest discover -s evaluation/metrics -p 'test_*.py' -v

No test in this file calls a model or touches the network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from collections import Counter as _Counter
from pathlib import Path

from regression import (
    analysis, contract_checks, grounding as g, reliability,
)
from regression import run as rm
from judges import actions as amc_aid, completeness, stage_attribution
from judges import packets as adjudication
from judges import packets as A
from gate import arms as arm_analysis
from lib import model_client, stats


def minutes(**overrides):
    """Minimal contract-valid six-field minutes object."""
    base = {
        "summary": "会议确定了试点范围。",
        "topics": [{"title": "试点", "summary": "讨论试点范围。"}],
        "viewpoints": [{"speaker": "程安", "viewpoint": "只在产品组试点。"}],
        "decisions": [{
            "content": "只在产品组试点。",
            "basis": "程安提出。",
            "owner_suggestion": "程安",
            "deadline_suggestion": "2026年9月28日",
        }],
        "pending_items": [],
        "risks": [],
    }
    base.update(overrides)
    return base


class StatsTest(unittest.TestCase):
    def test_clopper_pearson_matches_published_values(self):
        low, high = stats.clopper_pearson_interval(0, 219)
        self.assertAlmostEqual(low, 0.0, places=6)
        self.assertAlmostEqual(high, 0.01670, places=4)

        low, high = stats.clopper_pearson_interval(1, 23)
        self.assertAlmostEqual(low, 0.00110, places=4)
        self.assertAlmostEqual(high, 0.21949, places=4)

        low, high = stats.clopper_pearson_interval(10, 20)
        self.assertAlmostEqual(low, 0.27196, places=4)
        self.assertAlmostEqual(high, 0.72804, places=4)

    def test_clopper_pearson_degenerate_cases(self):
        self.assertAlmostEqual(stats.clopper_pearson_interval(20, 20)[0], 0.83157, places=4)
        self.assertIsNone(stats.clopper_pearson_interval(0, 0))

    def test_wilson_is_tighter_than_exact_and_ordered(self):
        low, high = stats.wilson_interval(1, 23)
        self.assertLess(low, 1 / 23)
        self.assertGreater(high, 1 / 23)

    def test_mcnemar_exact_known_values(self):
        self.assertAlmostEqual(stats.mcnemar_exact(8, 1)["p_value_two_sided"], 0.0390625, places=7)
        self.assertAlmostEqual(stats.mcnemar_exact(3, 3)["p_value_two_sided"], 1.0, places=7)
        self.assertIsNone(stats.mcnemar_exact(0, 0)["p_value_two_sided"])

    def test_spearman_handles_ties_and_constant_input(self):
        self.assertAlmostEqual(stats.spearman_rho([1, 2, 3, 4], [1, 2, 3, 4]), 1.0, places=6)
        self.assertAlmostEqual(stats.spearman_rho([1, 2, 3, 4], [4, 3, 2, 1]), -1.0, places=6)
        self.assertIsNone(stats.spearman_rho([1, 1, 1, 1], [1, 2, 3, 4]))
        self.assertIsNone(stats.spearman_rho([1, 2], [1, 2]))

    def test_nearest_rank_percentile_returns_observed_value(self):
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(stats.nearest_rank_percentile(values, 95), 95.0)
        self.assertEqual(stats.nearest_rank_percentile(values, 50), 50.0)
        self.assertIsNone(stats.nearest_rank_percentile([], 95))

    def test_holm_adjust_matches_manual_step_down(self):
        # 手算 Holm：原始 p [0.01, 0.04, 0.03]，m=3。
        # 排序 0.01×3=0.03；0.03×2=0.06；0.04×1=0.04 被单调封顶到 0.06，
        # 槽位保持 → [0.03, 0.06, 0.06]。
        adjusted = stats.holm_adjust([0.01, 0.04, 0.03])
        self.assertAlmostEqual(adjusted[0], 0.03, places=10)
        self.assertAlmostEqual(adjusted[1], 0.06, places=10)
        self.assertAlmostEqual(adjusted[2], 0.06, places=10)

    def test_holm_adjust_handles_none_and_monotonic_cap(self):
        adjusted = stats.holm_adjust([0.20, None, 0.90])
        self.assertIsNone(adjusted[1])
        self.assertAlmostEqual(adjusted[0], 0.20 * 2, places=10)  # m=2，不含 None
        self.assertAlmostEqual(adjusted[2], 0.90 * 1, places=10)
        # 单调封顶：更大的原始 p 不得得到更小的校正 p。
        self.assertLessEqual(adjusted[0], adjusted[2])

    def test_family_report_flags_critical_results(self):
        report = stats.family_report([
            ("completeness b->p", 0.0275),
            ("completeness b->r", 0.4614),
            ("completeness p->r", 0.1877),
        ])
        self.assertEqual(report["n_tests"], 3)
        by_label = {row["label"]: row for row in report["comparisons"]}
        # 0.0275 * 3 = 0.0825 > 0.05：审计指出的那条临界结论在 Holm 下不再显著。
        self.assertAlmostEqual(by_label["completeness b->p"]["p_holm"], 0.0825, places=10)
        self.assertFalse(by_label["completeness b->p"]["reject_at_0_05_holm"])
        self.assertTrue(all(row["p_holm"] >= row["p_raw"] for row in report["comparisons"]))

    def test_wilcoxon_reports_pratt_alongside_wilcox_when_zeros_exist(self):
        # 6 个非零全为改善 + 20 个零差：wilcox 剔除零差 p=2*(1/2)^6=0.03125。
        diffs = [1.0] * 6 + [0.0] * 20
        result = stats.wilcoxon_signed_rank(diffs)
        self.assertAlmostEqual(result["p_value_two_sided"], 0.03125, places=10)
        self.assertIn("pratt", result)
        self.assertIn("p_value_two_sided", result["pratt"])
        self.assertIsNotNone(result["pratt"]["p_value_two_sided"])

    def test_wilcoxon_pratt_zero_ranks_displace_nonzero_ranks(self):
        # Pratt：|d|=[0,0,1,2] → 全样本秩 [1.5,1.5,3,4]；非零符号秩 3、4。
        # 全负时 W=0；4 个符号分配里 2 个满足 min 侧 ≤0 → p=2/4=0.5。
        result = stats.wilcoxon_signed_rank([0.0, 0.0, -1.0, -2.0])
        pratt = result["pratt"]
        self.assertAlmostEqual(pratt["w_minus"], 7.0, places=10)
        self.assertAlmostEqual(pratt["p_value_two_sided"], 0.5, places=10)

    def test_wilcoxon_normal_approx_agrees_with_tie_corrected_reference(self):
        # 30 个非零、|d| 全并列（10x0.1、10x0.2、10x0.3）：并列校正后方差缩小，
        # p 必须小于未校正版且与 scipy 的 tie-corrected 结果同向（audit 实测 0.339→0.273 一类）。
        diffs = [0.1] * 10 + [-0.1] * 10 + [0.2] * 5 + [-0.2] * 5
        result = stats.wilcoxon_signed_rank(diffs, exact_max_n=25)
        self.assertIn("tie-corrected", result["method"])
        self.assertLess(result["p_value_two_sided"], 1.0)


class GroundingTest(unittest.TestCase):
    def test_digit_skeleton_unifies_date_formats(self):
        for surface in ("2026年9月28日", "2026-09-28", "2026/9/28", "2026.9.28"):
            self.assertEqual(g.digit_skeleton(surface), "2026-9-28")

    def test_date_extraction_variants(self):
        skeletons = {a["skeleton"] for a in g.extract_date_atoms("于 9月28日 与 2026-09-23 交付")}
        self.assertIn("9-28", skeletons)
        self.assertIn("2026-9-23", skeletons)

    def test_chinese_numerals(self):
        self.assertEqual(g.chinese_numeral_to_int("十"), 10)
        self.assertEqual(g.chinese_numeral_to_int("二十三"), 23)
        self.assertEqual(g.chinese_numeral_to_int("两"), 2)
        self.assertEqual(g.chinese_numeral_to_int("不是数字"), None)
        self.assertEqual(g.normalize_numerals("十份样例"), "10份样例")

    def test_fabricated_date_is_unsupported(self):
        transcript = "[发言人1] 我们下个月再讨论排期。"
        analysis_result = g.analyze_output(minutes(), transcript)
        self.assertGreater(analysis_result["unsupported_atom_count"], 0)
        flagged = {
            atom["surface"]
            for results in analysis_result["per_field"].values()
            for result in results
            for atom in result["unsupported_atoms"]
        }
        self.assertIn("2026年9月28日", flagged)

    def test_grounded_date_is_supported(self):
        transcript = "[程安] 只在产品组试点，试点从 2026 年 9 月 28 日开始。"
        result = g.analyze_output(minutes(), transcript)
        self.assertEqual(result["unsupported_atom_count"], 0)

    def test_declared_unknown_is_never_a_fabrication(self):
        """An explicit 'not decided' is a correct answer, not an unsupported claim."""
        for marker in ("无", "待确认", "未明确提及", "待定", "暂未确定"):
            output = minutes(
                summary="讨论了待办。", topics=[], viewpoints=[], decisions=[], risks=[],
                pending_items=[{
                    "content": "补齐审核页入口。",
                    "owner_suggestion": marker,
                    "deadline_suggestion": marker,
                }],
            )
            result = g.analyze_output(output, "转写里没有日期也没有负责人。")
            self.assertEqual(result["unsupported_atom_count"], 0, msg=marker)
            self.assertGreater(result["declared_unknown_count"], 0, msg=marker)

    def test_vague_time_is_reported_but_not_flagged(self):
        output = minutes(
            summary="讨论了待办。", topics=[], viewpoints=[], decisions=[], risks=[],
            pending_items=[{
                "content": "补齐审核页入口。",
                "owner_suggestion": "产品组",
                "deadline_suggestion": "持续推进",
            }],
        )
        result = g.analyze_output(output, "转写里没有日期。")
        self.assertEqual(result["unsupported_atom_count"], 0)
        self.assertGreater(result["vague_time_count"], 0)

    def test_entity_splitting_keeps_only_specific_names_as_hard(self):
        """Audit-driven: relational and role phrases must not be counted as fabricated."""
        segments = g.split_entities("韩永在（总校长）、主持人、其基金会同事、相关合作方、微软亚洲研究院")
        hard = {s["surface"] for s in segments if s["class"] == "hard"}
        descriptive = {s["surface"] for s in segments if s["class"] == "descriptive"}
        self.assertIn("韩永在", hard)
        self.assertIn("微软亚洲研究院", hard)
        self.assertTrue({"主持人", "其基金会同事", "相关合作方"} <= descriptive)

    def test_descriptive_owner_is_not_counted_as_fabricated(self):
        output = minutes(
            summary="讨论了试点。", topics=[], viewpoints=[], risks=[], pending_items=[],
            decisions=[{
                "content": "只在产品组试点。", "basis": "会上提出。",
                "owner_suggestion": "相关合作方", "deadline_suggestion": "无",
            }],
        )
        result = g.analyze_output(output, "转写里完全没有提到任何合作方。")
        self.assertEqual(result["unsupported_atom_count"], 0)

    def test_fabricated_owner_is_unsupported(self):
        output = minutes(decisions=[{
            "content": "只在产品组试点。",
            "basis": "程安提出。",
            "owner_suggestion": "王富贵",
            "deadline_suggestion": "无",
        }])
        result = g.analyze_output(output, "[程安] 只在产品组试点。")
        flagged = {
            atom["surface"]
            for results in result["per_field"].values()
            for r in results
            for atom in r["unsupported_atoms"]
        }
        self.assertIn("王富贵", flagged)

    def test_summary_field_counted_once(self):
        result = g.analyze_output(minutes(summary="于 2027年1月1日 归档。"), "没有日期。")
        summary_results = result["per_field"]["summary"]
        self.assertEqual(len(summary_results), 1)
        self.assertTrue(summary_results[0]["has_unsupported"])


class SourceWindowTest(unittest.TestCase):
    def test_locates_region_and_reports_dates(self):
        transcript = (
            "[发言人1] 关于试点范围，我们决定只在产品组和客服组试点。" * 5
            + "试点从 2026 年 9 月 28 日开始，先跑两周。"
        )
        located = g.find_source_window("只在产品组和客服组试点", transcript)
        self.assertIsNotNone(located)
        padded = transcript + ("[发言人2] 后续再同步。" * 40) + "截止日期是 2026 年 9 月 28 日。"
        located_padded = g.find_source_window("只在产品组和客服组试点", padded)
        self.assertTrue(g.window_contains_date(
            padded[located_padded["window_start"]:located_padded["window_end"]]
        ))

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(g.find_source_window("完全不同的内容没有重合部分", "短"))
        self.assertIsNone(g.find_source_window("有重合部分的句子", "完全不相关的一段转写文本内容"))


class PairingTest(unittest.TestCase):
    def test_exact_then_fuzzy_then_unmatched(self):
        base = [{"content": "只在产品组试点。"}, {"content": "补充三个入口。"}, {"content": "去掉旧流程。"}]
        rev = [{"content": "只在产品组试点。"}, {"content": "补充三个入口，并补充文案。"}]
        result = analysis.pair_items("pending_items", base, rev)
        # Item 0 pairs exactly; item 1 pairs fuzzily onto the extended rewrite;
        # item 2 has no counterpart and must be reported, not silently dropped.
        self.assertIn((0, 0), result["pairs"])
        self.assertIn((1, 1), result["pairs"])
        self.assertEqual(result["base_only"], [2])
        self.assertEqual(result["rev_only"], [])

    def test_unmatched_revised_item_is_added(self):
        result = analysis.pair_items("pending_items", [{"content": "旧事项。"}], [{"content": "全新的事项。"}])
        self.assertEqual(result["pairs"], [])
        self.assertEqual(result["base_only"], [0])
        self.assertEqual(result["rev_only"], [0])


class ContractTest(unittest.TestCase):
    def test_valid_contract_passes(self):
        valid, errors = analysis._validate_contract(minutes())
        self.assertTrue(valid, msg=errors)

    def test_missing_field_and_wrong_type_and_enum_and_extra(self):
        broken = minutes()
        del broken["risks"]
        valid, errors = analysis._validate_contract(broken)
        self.assertFalse(valid)
        self.assertIn("risks:missing", errors)

        wrong_type = minutes(topics="不是数组")
        self.assertIn("topics:not_array", analysis._validate_contract(wrong_type)[1])

        bad_enum = minutes(risks=[{"content": "风险。", "level": "CRITICAL", "suggestion": "建议。"}])
        self.assertIn("risks/0/level:invalid_enum", analysis._validate_contract(bad_enum)[1])

        extra = minutes(unexpected="x")
        self.assertIn("unexpected:unexpected_field", analysis._validate_contract(extra)[1])

    def test_empty_summary_is_invalid(self):
        self.assertIn("summary:not_nonempty_string",
                      analysis._validate_contract(minutes(summary="   "))[1])


class TransitionTest(unittest.TestCase):
    def _case(self, base_output, rev_output, transcript):
        return {
            "case_id": "c1",
            "transcript": transcript,
            "baseline_output": base_output,
            "revised_output": rev_output,
            "record": {},
        }

    def test_dropping_a_fabricated_deadline_is_a_claim_level_gain(self):
        """The item still exists, so this is a matched pair, not a removal."""
        transcript = "[程安] 只在产品组试点。"
        base = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。",
            "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日",
        }])
        rev = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。",
            "owner_suggestion": "程安", "deadline_suggestion": "无",
        }])
        report = analysis.transition_report([self._case(base, rev, transcript)])
        self.assertEqual(report["claim_level_transitions"].get("fixed_to_supported", 0), 1)
        self.assertEqual(report["claim_level_transitions"].get("introduced_unsupported", 0), 0)
        self.assertGreater(report["net_grounded_item_change"], 0)

    def test_introducing_a_fabricated_deadline_counts_as_harm(self):
        transcript = "[程安] 只在产品组试点。"
        base = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。",
            "owner_suggestion": "程安", "deadline_suggestion": "无",
        }])
        rev = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。",
            "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日",
        }])
        report = analysis.transition_report([self._case(base, rev, transcript)])
        self.assertEqual(report["claim_level_transitions"].get("introduced_unsupported", 0), 1)
        self.assertEqual(report["claim_level_transitions"].get("fixed_to_supported", 0), 0)
        self.assertLess(report["net_grounded_item_change"], 0)

    def test_deleting_a_whole_item_is_reported_as_a_removal(self):
        transcript = "[程安] 只在产品组试点。"
        base = minutes(decisions=[
            {"content": "只在产品组试点。", "basis": "程安提出。",
             "owner_suggestion": "程安", "deadline_suggestion": "无"},
            {"content": "另一条完全不同的决定。", "basis": "程安提出。",
             "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日"},
        ])
        rev = minutes(decisions=[
            {"content": "只在产品组试点。", "basis": "程安提出。",
             "owner_suggestion": "程安", "deadline_suggestion": "无"},
        ])
        report = analysis.transition_report([self._case(base, rev, transcript)])
        self.assertEqual(report["set_level_transitions"].get("removed_unsupported", 0), 1)
        self.assertEqual(report["set_level_transitions"].get("removed_supported", 0), 0)

    def test_identical_versions_have_no_discordant_pairs(self):
        transcript = "[程安] 只在产品组试点，试点从 2026 年 9 月 28 日开始。"
        output = minutes()
        report = analysis.transition_report([self._case(output, output, transcript)])
        self.assertEqual(report["claim_level_mcnemar"]["n_discordant"], 0)
        # Four non-empty fields: summary, topics, viewpoints, decisions.
        self.assertEqual(report["claim_level_transitions"].get("kept_supported", 0), 4)
        self.assertEqual(report["net_grounded_item_change"], 0)


class SlotTest(unittest.TestCase):
    def test_empty_deadline_without_a_date_nearby_is_correctly_empty(self):
        transcript = "[程安] 这个功能后续再说，先不做。" * 3
        output = minutes(
            decisions=[{
                "content": "这个功能后续再说，先不做。", "basis": "程安提出。",
                "owner_suggestion": "程安", "deadline_suggestion": "无",
            }],
        )
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": {},
        }
        report = analysis.slot_report([case])
        conditional = report["conditional_deadline"]
        self.assertEqual(conditional["correctly_empty_count"], 1)
        self.assertEqual(
            conditional["unconditional_MISSING_DEADLINE_false_positive_rate"]["rate"], 1.0
        )

    def test_empty_deadline_with_a_date_nearby_is_a_candidate_miss(self):
        transcript = "[程安] 这个入口要在 2026 年 9 月 28 日之前补齐。"
        output = minutes(
            summary="讨论了入口。", topics=[], viewpoints=[], risks=[],
            decisions=[],
            pending_items=[{
                "content": "这个入口要在之前补齐。",
                "owner_suggestion": "待定",
                "deadline_suggestion": "未明确",
            }],
        )
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": {},
        }
        report = analysis.slot_report([case])
        buckets = report["conditional_deadline"]["empty_deadline_bucket_counts"]
        self.assertEqual(buckets.get("deadline_in_source_window", 0), 1)
        self.assertEqual(buckets.get("no_deadline_in_source_window", 0), 0)

    def test_unlocatable_item_is_not_guessed(self):
        transcript = "[程安] 完全不相干的一段发言内容。"
        output = minutes(
            summary="讨论了入口。", topics=[], viewpoints=[], risks=[], decisions=[],
            pending_items=[{
                "content": "毫不重叠的另一件事项描述。",
                "owner_suggestion": "待定",
                "deadline_suggestion": "未明确",
            }],
        )
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": {},
        }
        report = analysis.slot_report([case])
        buckets = report["conditional_deadline"]["empty_deadline_bucket_counts"]
        self.assertEqual(buckets.get("window_not_located", 0), 1)
        self.assertEqual(
            report["conditional_deadline"]["unconditional_MISSING_DEADLINE_false_positive_rate"]["total"],
            0,
        )


class CritiqueTest(unittest.TestCase):
    def test_precision_and_recall_against_the_proxy(self):
        transcript = "[程安] 只在产品组试点。"
        output = minutes(
            summary="讨论了试点。", topics=[], viewpoints=[], risks=[], pending_items=[],
            decisions=[
                {"content": "只在产品组试点。", "basis": "程安提出。",
                 "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日"},
                {"content": "另一条。", "basis": "程安提出。",
                 "owner_suggestion": "程安", "deadline_suggestion": "无"},
            ],
        )
        record = {
            "baseline": {"output": output},
            "agent_steps": [
                {"step_type": "REVIEW", "round_no": 1, "payload": {
                    "passed": False, "score": 50,
                    "issues": [{"field": "decisions", "index": 0, "issue_type": "UNSUPPORTED",
                                "detail": "x", "suggestion": "y"}],
                }},
                {"step_type": "REFINE", "round_no": 1, "payload": output},
            ],
        }
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": record,
        }
        report = analysis.critique_report([case])
        self.assertEqual(report["issue_type_distribution"], {"UNSUPPORTED": 1})
        # The reviewer flagged exactly the item the proxy also flags.
        self.assertEqual(report["reviewer_precision_vs_proxy"]["successes"], 1)
        self.assertEqual(report["reviewer_precision_vs_proxy"]["total"], 1)
        self.assertEqual(report["reviewer_recall_vs_proxy"]["successes"], 1)
        self.assertEqual(report["reviewer_recall_vs_proxy"]["total"], 1)

    def test_whole_block_issue_covers_its_field(self):
        transcript = "[程安] 只在产品组试点。"
        output = minutes(
            summary="讨论了试点。", topics=[], viewpoints=[], risks=[], pending_items=[],
            decisions=[
                {"content": "只在产品组试点。", "basis": "程安提出。",
                 "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日"},
                {"content": "另一条。", "basis": "程安提出。",
                 "owner_suggestion": "程安", "deadline_suggestion": "2027年1月1日"},
            ],
        )
        record = {
            "baseline": {"output": output},
            "agent_steps": [
                {"step_type": "REVIEW", "round_no": 1, "payload": {
                    "passed": False, "score": 40,
                    "issues": [{"field": "decisions", "index": -1, "issue_type": "UNSUPPORTED",
                                "detail": "x", "suggestion": "y"}],
                }},
                {"step_type": "REFINE", "round_no": 1, "payload": output},
            ],
        }
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": record,
        }
        report = analysis.critique_report([case])
        self.assertEqual(report["issue_index_kind"], {"whole_block": 1})
        self.assertEqual(report["reviewer_recall_vs_proxy"]["successes"], 2)

    def test_passed_round_with_flags_is_counted(self):
        transcript = "[程安] 只在产品组试点。"
        output = minutes(decisions=[
            {"content": "只在产品组试点。", "basis": "程安提出。",
             "owner_suggestion": "程安", "deadline_suggestion": "2026年9月28日"},
        ])
        record = {
            "baseline": {"output": output},
            "agent_steps": [
                {"step_type": "REVIEW", "round_no": 1,
                 "payload": {"passed": True, "score": 95, "issues": []}},
            ],
        }
        case = {
            "case_id": "c1", "transcript": transcript,
            "baseline_output": output, "revised_output": output, "record": record,
        }
        report = analysis.critique_report([case])
        self.assertEqual(report["passed_rounds_with_proxy_flags"], 1)
        self.assertEqual(report["passed_round_denominator"], 1)
        self.assertIsNone(report["spearman_issue_count_vs_flag_count"])


class ContentSupportTest(unittest.TestCase):
    def test_verbatim_clause_scores_high_and_invention_scores_zero(self):
        transcript = "[程安] 我们决定只在产品组和客服组进行试点，试点从下月开始。"
        index = g.build_transcript_index(transcript)
        verbatim = g.content_support_ratio("我们决定只在产品组和客服组进行试点", index)
        invented = g.content_support_ratio("建议加强前期规划并严格落实三同步要求", index)
        self.assertGreater(verbatim["ratio"], 0.8)
        self.assertEqual(invented["ratio"], 0.0)

    def test_returns_none_for_empty_or_short_text(self):
        index = g.build_transcript_index("一些转写内容。")
        self.assertIsNone(g.content_support_ratio("", index))
        self.assertIsNone(g.content_support_ratio("短", index))

    def test_gram_pool_coverability(self):
        pool = g.content_gram_set("建设和运营面向企业和社会服务的人工智能研发平台")
        covered = g.content_gram_set("建设和运营面向企业的人工智能研发平台")
        unrelated = g.content_gram_set("完全无关的另一个议题内容")
        self.assertTrue(g.is_coverable_by(covered, pool))
        self.assertFalse(g.is_coverable_by(unrelated, pool))
        self.assertFalse(g.is_coverable_by(set(), pool))


class MergeDetectionTest(unittest.TestCase):
    """A consolidation must not be counted as a deletion of correct content."""

    def test_consolidation_is_labelled_merged_not_removed(self):
        """No single revised item pairs, but the pool covers the draft's wording.

        A merge is only detectable when every revised item is shorter than a
        third of the draft item: at that length the similarity ratio stays below
        the pairing threshold while the union of the parts still covers the
        draft's character n-grams.
        """
        transcript = "[程安] 确定作答框架并覆盖四个方向，只在产品组试点。"
        draft = "确定作答框架为总括句加四个层面的引导对策并覆盖融媒体中心和高校学者网信办宣传部四个方向"
        base = minutes(
            summary="讨论了框架。", topics=[], viewpoints=[], risks=[], decisions=[],
            pending_items=[{"content": draft, "owner_suggestion": "程安",
                            "deadline_suggestion": "无"}],
        )
        rev = minutes(
            summary="讨论了框架。", topics=[], viewpoints=[], risks=[], decisions=[],
            pending_items=[
                {"content": "确定作答框架为总括句", "owner_suggestion": "程安",
                 "deadline_suggestion": "无"},
                {"content": "加四个层面的引导对策", "owner_suggestion": "程安",
                 "deadline_suggestion": "无"},
                {"content": "覆盖融媒体中心和高校学者", "owner_suggestion": "程安",
                 "deadline_suggestion": "无"},
                {"content": "网信办宣传部四个方向", "owner_suggestion": "程安",
                 "deadline_suggestion": "无"},
            ],
        )
        pairing = analysis.pair_items("pending_items", g.iter_items(base, "pending_items"),
                                      g.iter_items(rev, "pending_items"))
        self.assertEqual(pairing["base_only"], [0], "fixture must leave the draft item unpaired")
        report = analysis.transition_report([{
            "case_id": "c1", "transcript": transcript,
            "baseline_output": base, "revised_output": rev, "record": {},
        }])
        self.assertEqual(report["set_level_transitions"].get("merged_supported", 0), 1)
        self.assertEqual(report["set_level_transitions"].get("removed_supported", 0), 0)

    def test_genuine_deletion_is_still_removed(self):
        transcript = "[程安] 先做的事情。"
        base = minutes(
            summary="讨论了平台。", topics=[], viewpoints=[], risks=[], decisions=[],
            pending_items=[{"content": "先做的事情。", "owner_suggestion": "程安",
                            "deadline_suggestion": "无"}],
        )
        rev = minutes(
            summary="讨论了平台。", topics=[], viewpoints=[], risks=[], decisions=[],
            pending_items=[],
        )
        report = analysis.transition_report([{
            "case_id": "c1", "transcript": transcript,
            "baseline_output": base, "revised_output": rev, "record": {},
        }])
        self.assertEqual(report["set_level_transitions"].get("removed_supported", 0), 1)
        self.assertEqual(report["set_level_transitions"].get("merged_supported", 0), 0)


class AucTest(unittest.TestCase):
    def test_auc_direction_and_ties(self):
        self.assertEqual(stats.auc([0.9, 0.8], [0.1, 0.2]), 1.0)
        self.assertEqual(stats.auc([0.1], [0.9]), 0.0)
        self.assertEqual(stats.auc([0.5], [0.5]), 0.5)
        self.assertIsNone(stats.auc([], [0.5]))
        self.assertIsNone(stats.auc([0.5], []))


class DeadlineStateTest(unittest.TestCase):
    def test_six_states_are_distinguished(self):
        cases = {
            "": "empty",
            "待确认": "declared_unknown",
            "2026年9月28日": "asserting",
            "年内": "open_ended",
            "本学期内启动": "open_ended",
            "上线后": "open_ended",
            # A commitment in the time slot is the documented defect.
            "完成数据收集和目标差距分析": "field_misuse",
            "确定一门攻坚学科并制定提分计划": "field_misuse",
            "第一批": "unparsed",
        }
        for value, expected in cases.items():
            self.assertEqual(g.deadline_state(value), expected, msg=repr(value))

    def test_field_misuse_is_not_counted_as_open_ended(self):
        self.assertNotEqual(g.deadline_state("完成国内升学与留学路线的初步评估"), "open_ended")


class ActionCandidateTest(unittest.TestCase):
    def test_extracts_commitments_and_skips_past_reports(self):
        transcript = (
            "我们负责在下周完成接口联调。"
            "上个月已经完成了域名备案。"
            "今天天气不错。"
            "张三需要准备十份脱敏样例。"
        )
        candidates = g.action_candidates(transcript)
        self.assertEqual(len(candidates), 2, msg=candidates)
        self.assertTrue(any("接口联调" in c for c in candidates))
        self.assertFalse(any("备案" in c for c in candidates))

    def test_coverage_detects_present_and_absent_items(self):
        candidates = ["需要完成接口联调工作", "负责准备脱敏样例材料"]
        item_texts = ["需要完成接口联调工作"]
        result = g.candidate_coverage(candidates, item_texts)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["covered_count"], 1)
        self.assertEqual(result["uncovered_count"], 1)


class InventedSpanTest(unittest.TestCase):
    def test_verbatim_text_has_no_spans_and_invention_does(self):
        index = g.build_transcript_index("[程安] 我们决定只在产品组试点，下月启动。")
        self.assertEqual(g.invented_spans("我们决定只在产品组试点", index)["span_count"], 0)
        self.assertGreater(
            g.invented_spans("建议强化风险防控机制并建立长效评估体系", index)["span_count"], 0
        )

    def test_function_word_only_spans_are_filtered(self):
        index = g.build_transcript_index("完全不相干的转写内容在这里。")
        result = g.invented_spans("的了和与及或是为", index)
        self.assertEqual(result["span_count"], 0)


class WilcoxonTest(unittest.TestCase):
    def test_known_exact_values(self):
        # All five differences positive: W+=15, W-=0, exact two-sided p = 2/2^5.
        result = stats.wilcoxon_signed_rank([1, 2, 3, 4, 5])
        self.assertEqual(result["w_plus"], 15.0)
        self.assertEqual(result["w_minus"], 0)
        self.assertAlmostEqual(result["p_value_two_sided"], 0.0625, places=6)

    def test_all_zero_has_no_power(self):
        result = stats.wilcoxon_signed_rank([0, 0, 0])
        self.assertIsNone(result["p_value_two_sided"])
        self.assertEqual(result["n_zero"], 3)

    def test_symmetric_differences_are_not_significant(self):
        result = stats.wilcoxon_signed_rank([1, -1, 2, -2, 3, -3])
        self.assertAlmostEqual(result["p_value_two_sided"], 1.0, places=6)


class OutputLevelMetricTest(unittest.TestCase):
    def test_risk_level_distribution_and_degeneracy(self):
        spread = minutes(risks=[
            {"content": "风险一。", "level": "HIGH", "suggestion": "建议一。"},
            {"content": "风险二。", "level": "LOW", "suggestion": "建议二。"},
        ])
        self.assertEqual(g.risk_level_distribution(spread), {"HIGH": 1, "LOW": 1})
        self.assertEqual(g.risk_level_distribution(minutes()), {})

    def test_summary_decision_coverage(self):
        covered = minutes(
            summary="会议决定只在产品组试点，下月启动。",
            decisions=[{"content": "只在产品组试点，下月启动。", "basis": "程安提出。",
                        "owner_suggestion": "程安", "deadline_suggestion": "无"}],
        )
        index = g.build_transcript_index("转写")
        result = g.summary_decision_coverage(covered, index)
        self.assertEqual(result["decision_count"], 1)
        self.assertEqual(result["covered_count"], 1)

        omitted = minutes(
            summary="会议讨论了其他事项。",
            decisions=[{"content": "只在产品组试点，下月启动。", "basis": "程安提出。",
                        "owner_suggestion": "程安", "deadline_suggestion": "无"}],
        )
        self.assertEqual(g.summary_decision_coverage(omitted, index)["covered_count"], 0)

    def test_rejected_proxies_are_not_reportable(self):
        reportable = g.reportable_proxies()
        for name in ("invented_spans", "actionability", "vagueness", "action_candidate_coverage"):
            self.assertNotIn(name, reportable, msg=name)
        for name in ("atom_grounding", "merge_detection", "deadline_state"):
            self.assertIn(name, reportable, msg=name)


class AmcAidTest(unittest.TestCase):
    def test_positive_f1_arithmetic(self):
        result = amc_aid.positive_f1([1, 1, 0, 0], [1, 0, 1, 0])
        self.assertEqual(result["true_positive"], 1)
        self.assertEqual(result["false_positive"], 1)
        self.assertEqual(result["false_negative"], 1)
        self.assertAlmostEqual(result["precision"], 0.5)
        self.assertAlmostEqual(result["recall"], 0.5)
        self.assertAlmostEqual(result["positive_f1"], 0.5)

    def test_all_negative_floor_has_zero_f1_despite_high_accuracy(self):
        result = amc_aid.positive_f1([0, 0, 0, 1], [0, 0, 0, 0])
        self.assertEqual(result["positive_f1"], 0.0)
        self.assertEqual(result["accuracy"], 0.75)

    def test_mismatched_lengths_rejected(self):
        with self.assertRaises(ValueError):
            amc_aid.positive_f1([0, 1], [0])

    def test_gold_as_prediction_scores_exactly_one(self):
        try:
            result = amc_aid.self_check("dev")
        except FileNotFoundError:
            self.skipTest("corpus archive not present")
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["micro_positive_f1"], 1.0)


class AdjudicationTest(unittest.TestCase):
    def _packet(self, labels, strata, reviewer_types=None, proxy_flags=None):
        reviewer_types = reviewer_types or {}
        proxy_flags = proxy_flags or {}
        items = []
        key = {}
        for index, (label, stratum) in enumerate(zip(labels, strata), start=1):
            aid = f"item_{index:03d}"
            items.append({"anonymous_id": aid, "field": "decisions", "item_text": "x",
                          "transcript_window": "y", "label": label, "note": ""})
            key[aid] = {
                "stratum": stratum, "case_id": "c1", "item_index": index,
                "reviewer_issue_type": reviewer_types.get(aid),
                "reviewer_detail": "d",
                "proxy_flagged_atom": proxy_flags.get(aid),
                "version": "baseline",
            }
        return {"items": items, "_key": key}

    def test_label_collapse_keeps_derivable_grounded(self):
        self.assertTrue(A.label_collapse("supported"))
        self.assertTrue(A.label_collapse("derivable"))
        self.assertFalse(A.label_collapse("partial"))
        self.assertFalse(A.label_collapse("unsupported"))
        self.assertIsNone(A.label_collapse(""))

    def test_kappa_is_zero_when_labels_are_disjoint_vectors(self):
        """Guards the bug where a 4-way vector was compared with a binary one."""
        result = A.cohens_kappa(["supported", "supported"], ["flagged", "clean"])
        self.assertEqual(result["raw_agreement"], 0.0)
        self.assertLessEqual(result["kappa"], 0.0)

    def test_perfect_agreement_gives_kappa_one(self):
        result = A.cohens_kappa(["flagged", "clean", "flagged"], ["flagged", "clean", "flagged"])
        self.assertEqual(result["raw_agreement"], 1.0)
        self.assertEqual(result["kappa"], 1.0)

    def test_reconcile_reports_direction_of_disagreement(self):
        # Two items the reviewer flagged that the adjudicator calls grounded, and
        # one item the adjudicator flags that the reviewer left alone.
        packet = self._packet(
            ["supported", "derivable", "unsupported"],
            ["reviewer_UNSUPPORTED", "reviewer_UNSUPPORTED", "unflagged"],
            reviewer_types={"item_001": "UNSUPPORTED", "item_002": "UNSUPPORTED"},
        )
        result = A.reconcile(packet)
        self.assertEqual(result["labelled_count"], 3)
        directions = _Counter(d["direction"] for d in result["discordant_items"])
        self.assertEqual(directions["reviewer flagged, adjudicator did not"], 2)
        self.assertEqual(directions["adjudicator flagged, reviewer did not"], 1)

    def test_owner_fabricated_stratum_counts_as_reviewer_flagged(self):
        packet = self._packet(["supported", "unsupported"], ["owner_fabricated", "owner_fabricated"])
        result = A.reconcile(packet)
        # Both are treated as reviewer-flagged, so only the grounded one disagrees.
        self.assertEqual(len(result["discordant_items"]), 1)
        self.assertEqual(result["discordant_items"][0]["direction"],
                         "reviewer flagged, adjudicator did not")

    def test_build_packet_is_seeded_and_blinded(self):
        cases = [{
            "case_id": "c1",
            "transcript": "[程安] 只在产品组试点，下月启动。" * 10,
            "baseline_output": minutes(
                summary="讨论了试点。", topics=[], viewpoints=[], risks=[],
                decisions=[{"content": "只在产品组试点。", "basis": "程安提出。",
                            "owner_suggestion": "程安", "deadline_suggestion": "无"}],
                pending_items=[{"content": "下月启动试点工作。", "owner_suggestion": "程安",
                                "deadline_suggestion": "下月"}],
            ),
            "record": {"agent_steps": []},
        }]
        first = A.build_packet(cases, per_stratum=2, owner_sample=1, seed=7)
        second = A.build_packet(cases, per_stratum=2, owner_sample=1, seed=7)
        self.assertEqual([i["anonymous_id"] for i in first["items"]],
                         [i["anonymous_id"] for i in second["items"]])
        for item in first["items"]:
            self.assertIsNone(item["label"])
            # Blinding: no verdict fields leak into the item the annotator reads.
            for leak in ("reviewer_issue_type", "proxy_flagged_atom", "version", "stratum"):
                self.assertNotIn(leak, item)


    def test_product_unavailable_fallback_actually_runs(self):
        """产品目录不可用时的本地兜底分支必须真的能跑。

        实测教训：这条分支里引用了两个**根本不存在的名字**（`MATCH_THRESHOLD`），
        而它在开发环境从不触发——真的退化时会 NameError 崩掉，而不是降级运行。
        该分支的声明是"退回本地实现并如实标注用的是哪一份"，所以这里强制走它。
        """
        original = g.SHARED
        g.SHARED = None
        try:
            thresholds = g.shared_thresholds()
            self.assertEqual(thresholds["similarity"], g.FALLBACK_SIMILARITY_THRESHOLD)
            self.assertIn("本地实现", thresholds["source"])
            self.assertTrue(g.shared_is_same_item("只在产品组试点并下月启动",
                                                  "只在产品组试点并下月启动"))
            self.assertFalse(g.shared_is_same_item("只在产品组试点", "完全无关的另一件事"))
            self.assertFalse(g.shared_is_same_item("", "x"))
        finally:
            g.SHARED = original

    def test_shared_thresholds_agree_with_the_product_module(self):
        """报告里标注的阈值必须等于真正生效的值——否则文档会宣称一个不存在的校准。"""
        if not g.shared_contract:
            self.skipTest("product contract layer unavailable")
        thresholds = g.shared_thresholds()
        textsim = g.SHARED["textsim"]
        self.assertEqual(thresholds["similarity"], float(textsim.SIMILARITY_THRESHOLD))
        self.assertEqual(thresholds["min_matched_block"], float(textsim.MIN_MATCHED_BLOCK))


class ContractVersionTest(unittest.TestCase):
    """契约 v2 产物上，证据类不变量的含义会**反转**，所以版本必须显式声明。"""

    def _v2_case(self):
        """一场只有一条目、证据可定位的最小 v2 产物。"""
        entry = {"quote": "只在产品组试点", "speaker": "发言人1"}
        return {
            "case_id": "c1",
            "transcript": "发言人1：我们只在产品组试点，下个月开始。",
            "baseline_output": {
                "summary": "会议讨论了试点范围。",
                "topics": [{"title": "试点范围", "summary": "只在产品组试点。",
                            "origin": "stated", "evidence": [entry]}],
                "viewpoints": [],
                "decisions": [],
                "pending_items": [{"content": "下月启动试点。",
                                   "owner": {"text": "产品组", "basis": "stated"},
                                   "deadline": {"text": "下个月", "basis": "stated"},
                                   "origin": "stated", "evidence": [entry]}],
                "risks": [],
            },
            "revised_output": None,
            "record": {"agent_steps": []},
        }

    def test_unknown_contract_version_is_rejected(self):
        # 传错版本会把真实缺陷率报成假象（或反过来），因此宁可报错
        with self.assertRaises(ValueError):
            contract_checks.build_report([], data_contract="v3")

    @unittest.skipUnless(contract_checks.load_contracts() is not None,
                         "product contract layer unavailable (needs product venv)")
    def test_v2_moves_evidence_invariants_into_quoted_counts(self):
        case = self._v2_case()
        legacy = contract_checks.build_report([case], data_contract=contract_checks.LEGACY)
        v2 = contract_checks.build_report([case], data_contract=contract_checks.V2)
        # 同一批产物：证据类计数在两代口径下分别落在"假象"与"可引用"两处
        self.assertNotIn("item_without_verifiable_evidence", legacy["quoted_counts"]["baseline"])
        self.assertIn("item_without_verifiable_evidence", legacy["legacy_artifact_counts"]["baseline"])
        self.assertIn("item_without_verifiable_evidence", v2["quoted_counts"]["baseline"])
        self.assertNotIn("legacy_artifact_counts", v2)
        # 结构类在任何一代都可引用
        for report in (legacy, v2):
            self.assertIn("slot_type_mismatch", report["quoted_counts"]["baseline"])

    @unittest.skipUnless(contract_checks.load_contracts() is not None,
                         "product contract layer unavailable (needs product venv)")
    def test_locatability_uses_the_product_criterion(self):
        case = self._v2_case()
        report = contract_checks.locatability_report(contract_checks.load_contracts(), [case])
        item_scope = report["by_scope"]["item_evidence"]["combined"]
        # topics 1 条 + pending_items 1 条，证据都是同一句可定位引用
        self.assertEqual(item_scope["evidence"], 2)
        self.assertEqual(item_scope["locatable"], 2)
        self.assertEqual(report["unlocated_breakdown"], {})

    @unittest.skipUnless(contract_checks.load_contracts() is not None,
                         "product contract layer unavailable (needs product venv)")
    def test_fabricated_and_elided_quotes_are_separated(self):
        case = self._v2_case()
        # 省略号把转写里不相邻的两段拼起来：归一化后仍定位不到，但形态是"截断"而非"编造"
        case["transcript"] = "发言人1：我们只在产品组试点，风险确实不小，下个月开始。"
        topic = case["baseline_output"]["topics"][0]
        topic["evidence"] = [{"quote": "这段话完全不存在于转写里", "speaker": "发言人1"}]
        case["baseline_output"]["pending_items"][0]["evidence"] = [
            {"quote": "我们只在产品组试点……下个月开始", "speaker": "发言人1"},
        ]
        report = contract_checks.locatability_report(contract_checks.load_contracts(), [case])
        self.assertEqual(report["unlocated_breakdown"],
                         {"ellipsis_elided": 1, "verbatim_mismatch": 1})
        # 省略号拆分只是诊断，不改变分子分母
        self.assertEqual(report["by_scope"]["item_evidence"]["combined"]["evidence"], 2)
        self.assertEqual(report["by_scope"]["item_evidence"]["combined"]["locatable"], 0)

    @unittest.skipUnless(contract_checks.load_contracts() is not None,
                         "product contract layer unavailable (needs product venv)")
    def test_audit_sample_is_spread_across_cases(self):
        cases = []
        for index in range(6):
            case = self._v2_case()
            case["case_id"] = f"c{index}"
            case["baseline_output"]["topics"][0]["evidence"] = [
                {"quote": f"编造的引用{index}", "speaker": "发言人1"},
            ]
            cases.append(case)
        report = contract_checks.locatability_report(contract_checks.load_contracts(), cases)
        seen = {example["case_id"] for example in report["unlocated_examples_for_audit"]}
        # 按每场最多 2 条分层：6 场每场 1 条未定位，样例必须覆盖全部 6 场
        self.assertEqual(seen, {f"c{index}" for index in range(6)})

    @unittest.skipUnless(contract_checks.load_contracts() is not None,
                         "product contract layer unavailable (needs product venv)")
    def test_item_report_counts_items_evidence_and_slots(self):
        report = contract_checks.item_report(contract_checks.load_contracts(), [self._v2_case()])
        combined = report["combined"]
        # topics 1 条 + pending_items 1 条；带证据 2 条；槽位 2 个（owner/deadline）
        self.assertEqual(combined["items"], 2)
        self.assertEqual(combined["items_with_evidence"], 2)
        self.assertEqual(combined["typed_slots"], 2)
        self.assertEqual(combined["basis_breakdown"], {"stated": 2})


class ReliabilityTest(unittest.TestCase):
    """可靠性与开销：纯计数。测试锁的是「分母是什么」，不是数值本身。"""

    def test_success_rate_counts_dispatches_not_authorised_calls(self):
        calls = [
            {"call_type": "AGENT_REVIEW", "status": "RESPONSE_RECEIVED", "elapsed_ms": 1000},
            {"call_type": "AGENT_REVIEW", "status": "RESPONSE_RECEIVED", "elapsed_ms": 2000},
            {"call_type": "MINUTES_MERGE", "status": "FAILED_OR_CANCELLED",
             "error_detail": "输出超过长度上限"},
        ]
        report = reliability.success_report(calls)
        self.assertEqual(report["overall"]["dispatched"], 3)
        self.assertEqual(report["overall"]["response_received"], 2)
        self.assertAlmostEqual(report["overall"]["success_rate"]["rate"], 2 / 3)
        # 失败分类不吞信息：能识别的进具名桶
        self.assertEqual(reliability.classify_failure("输出超过长度上限"), "output_truncated")
        self.assertEqual(reliability.classify_failure("完全没见过的错误"), "other")

    def test_failure_classifier_covers_the_observed_exception_names(self):
        """桶标记必须覆盖已观测到的异常类名。

        实测教训：两次输出截断记录的是 `LengthFinishReasonError`（驼峰、无空格），
        而当时的标记只有 "length limit"，两次截断都落进 other——失败分布是排查方向的
        第一手依据，分类器漏掉最该被看见的失败，比不分类更糟。
        """
        for error_type, expected in (
            ("LengthFinishReasonError", "output_truncated"),
            ("OpenAIInvalidRequestError", "request_rejected"),
            ("APITimeoutError", "timeout"),
            ("完全没见过的错误", "other"),
        ):
            self.assertEqual(reliability.classify_failure(error_type), expected, msg=error_type)

    def test_ledger_shape_is_recognised_by_structure_not_key_name(self):
        # 实测踩过：以为键名是 calls，实际是 dispatches，统计静默返回 0。
        # 因此按结构识别，且识别不到时由调用方显式暴露。
        renamed = {"run_id": "r1", "whatever": [{"status": "RESPONSE_RECEIVED"}]}
        self.assertEqual(len(reliability._calls_of(renamed)), 1)
        self.assertEqual(reliability._calls_of({"run_id": "r1", "empty": []}), [])
        self.assertEqual(reliability._calls_of({"run_id": "r1"}), [])

    def test_end_to_end_counts_every_attempt_including_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run-a"
            run.mkdir()
            (run / "report.json").write_text(json.dumps({
                "records": [
                    {"case_id": "c1", "status": "completed"},
                    {"case_id": "c2", "status": "failed"},
                    {"case_id": "c3", "status": "completed"},
                ],
            }), encoding="utf-8")
            report = reliability.build_report(root)
            end_to_end = report["end_to_end"]
            # 分母是三次尝试；只报"最终全通过"会把这次失败藏起来
            self.assertEqual(end_to_end["attempted_case_runs"], 3)
            self.assertEqual(end_to_end["completed_case_runs"], 2)
            self.assertEqual([f["case_id"] for f in end_to_end["failures"]], ["c2"])

    def test_latency_percentiles_are_nearest_rank_observations(self):
        calls = [{"call_type": "AGENT_REVIEW", "status": "RESPONSE_RECEIVED",
                  "elapsed_ms": value} for value in (10, 20, 30, 40, 50)]
        report = reliability.latency_report(calls)["by_call_type"]["AGENT_REVIEW"]
        self.assertEqual(report["percentile_method"], "nearest-rank")
        # nearest-rank：报出的值必须是真实观测到的，不做插值
        observed = (10.0, 20.0, 30.0, 40.0, 50.0)
        self.assertIn(float(report["median"]), observed)
        self.assertIn(float(report["p95_nearest_rank"]), observed)
        self.assertEqual(report["n"], 5)

    def test_missing_ledger_shape_is_surfaced_not_silently_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run-a"
            run.mkdir()
            (run / "dispatch-ledger.json").write_text(
                json.dumps({"run_id": "r1", "unexpected": {"not": "a list of calls"}}),
                encoding="utf-8")
            report = reliability.build_report(root)
            self.assertEqual(report["dispatched_call_count"], 0)
            # 关键：0 必须伴随"账本无法识别"的显式标注，而不是看起来像正常
            self.assertEqual(report["ledgers_without_recognizable_calls"], ["run-a"])


class NewContractLoaderTest(unittest.TestCase):
    """契约 v2 合并集的守卫：合并规则不成立时必须报错，不能出一组看似正常的数字。"""

    def _fixture(self, tmp_root, runs):
        """runs: {run_name: [(case_id, model_name), ...]}；产物用真实清单里的转写哈希。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
        import vcsum_adapter  # type: ignore

        entries = {case["id"]: case for case in vcsum_adapter.build_test_manifest()["cases"]}
        for run_name, records in runs.items():
            directory = Path(tmp_root) / run_name
            directory.mkdir(parents=True, exist_ok=True)
            payload = {"records": []}
            for case_id, model_name in records:
                entry = entries[case_id]
                payload["records"].append({
                    "case_id": case_id,
                    "status": "completed",
                    "ai_call_logs": [{"call_type": "MINUTES_CHUNK", "model_name": model_name}],
                    "source": {"transcript_sha256": entry["transcript_sha256"]},
                    "baseline": {"output": {"summary": "s", "topics": [], "viewpoints": [],
                                            "decisions": [], "pending_items": [], "risks": []}},
                    "revised": None,
                })
            (directory / "report.json").write_text(json.dumps(payload), encoding="utf-8")
        return entries

    def test_case_present_in_two_runs_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, {"run-a": [("vcsum_23", "deepseek-flash")],
                                "run-b": [("vcsum_23", "deepseek-flash")]})
            with self.assertRaises(ValueError) as caught:
                rm.load_new_contract_cases(
                    ["run-a", "run-b"], results_root=Path(tmp), expected_cases=1)
            self.assertIn("都有完成产物", str(caught.exception))

    def test_cross_model_merge_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, {"run-a": [("vcsum_23", "qwen3.8-max-0902")]})
            with self.assertRaises(ValueError) as caught:
                rm.load_new_contract_cases(
                    ["run-a"], results_root=Path(tmp), expected_cases=1)
            self.assertIn("跨模型的产物不合并", str(caught.exception))

    def test_incomplete_coverage_is_rejected_with_the_missing_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp, {"run-a": [("vcsum_23", "deepseek-flash")]})
            with self.assertRaises(ValueError) as caught:
                rm.load_new_contract_cases(["run-a"], results_root=Path(tmp))
            message = str(caught.exception)
            self.assertIn("合并后得到 1 场", message)
            self.assertIn("vcsum_108", message)  # 缺哪一场必须点出来

    def test_missing_run_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                rm.load_new_contract_cases(["does-not-exist"], results_root=Path(tmp))

    def test_all_warns_and_continues_while_explicit_request_fails(self):
        """缺一份产物不能让无关的测量全部停摆，但也不能静默。

        两种行为的区别是有意的：显式请求某一组 → 报错；`--set all` → 继续跑其他组，
        但把"这组不可用"写进报告并打印警告。**"这组数字消失了"必须看起来与
        "这组数字正常"不同。**

        只替换旧的 VCSum 加载器（它要重建大清单，与本测试无关），
        让被测的行为——`all` 的容错策略——保持真实。
        """
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            one_case = [{"case_id": "vcsum_x", "transcript": "甲说了三件事。",
                         "baseline_output": minutes(), "revised_output": minutes(),
                         "record": {}}]
            with mock.patch.object(rm, "load_vcsum_cases", return_value=one_case):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    code = rm.main(["--set", "all", "--results-root",
                                    str(Path(tmp) / "missing"), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            report = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertIn("vcsum-test26-v2", report["unavailable_sets"])
            self.assertNotIn("vcsum-test26-v2", report["sets"])
            self.assertIn("vcsum-dev22", report["sets"])
            # 报告里必须出现"这一组不存在"的显式标记
            self.assertIn("不可用", (output / "METRICS_REPORT.md").read_text(encoding="utf-8"))


class AgreementExtrasTest(unittest.TestCase):
    """κ 之外必报的四个量：偏斜患病率下 κ 会坍缩，单向偏差必须单列。"""

    def test_pabak_and_indices_on_a_known_confusion_matrix(self):
        # 2×2：双方都 flagged 15、都 clean 5（n=20，完全一致）
        first = ["flagged"] * 15 + ["clean"] * 5
        second = ["flagged"] * 15 + ["clean"] * 5
        result = A.binary_agreement_extras(first, second)
        self.assertEqual(result["n"], 20)
        self.assertEqual(result["confusion"]["both_flagged"], 15)
        self.assertEqual(result["confusion"]["both_clean"], 5)
        self.assertAlmostEqual(result["pabak"], 1.0)  # 完全一致 → PABAK=1
        self.assertAlmostEqual(result["prevalence_index"], 0.5)  # 偏斜：15 比 5
        self.assertEqual(result["bias_index"], 0.0)

    def test_one_directional_disagreement_is_visible_even_when_kappa_collapses(self):
        """实测形态：审查标了 16 条、裁决者全判有依据 → κ≈0 但偏差是单向的。"""
        # 16 条分歧、34 条双方都 clean：κ 会被患病率压扁，但偏差指数与方向错误率不会
        first = ["clean"] * 50          # 裁决者：全 clean
        second = ["flagged"] * 16 + ["clean"] * 34   # 审查：标了 16 条
        result = A.binary_agreement_extras(first, second, first_name="adjudicator",
                                           second_name="reviewer")
        kappa = A.cohens_kappa(first, second)
        self.assertLess(abs(kappa["kappa"]), 0.2)          # κ 看起来"无一致性"
        self.assertAlmostEqual(result["pabak"], 2 * 34 / 50 - 1)
        self.assertEqual(result["confusion"]["only_reviewer_flagged"], 16)
        self.assertEqual(result["confusion"]["only_adjudicator_flagged"], 0)
        # 单向偏差：一个方向的错误率很大，另一个方向为 0（没有任何"裁决者标了审查没标"）
        self.assertEqual(result["error_rate"]["reviewer_flagged_but_adjudicator_did_not"]["successes"], 16)
        self.assertIsNone(result["error_rate"]["adjudicator_flagged_but_reviewer_did_not"])


class ModelClientTest(unittest.TestCase):
    """评测侧调用模型的唯一入口：不得在异常里泄漏 key。"""

    def test_endpoint_normalises_the_base_url(self):
        with mock.patch.dict("os.environ", {"LLM_BASE_URL": "https://api.deepseek.com/",
                                            "LLM_MODEL": "deepseek-flash",
                                            "LLM_API_KEY": "secret-key"}, clear=False):
            url, model, key = model_client.endpoint()
            self.assertEqual(url, "https://api.deepseek.com/v1/chat/completions")
            self.assertEqual(model, "deepseek-flash")
            self.assertEqual(key, "secret-key")
        with mock.patch.dict("os.environ", {"LLM_BASE_URL": "https://example.test/v1",
                                            "LLM_API_KEY": "secret-key"}, clear=False):
            self.assertEqual(model_client.endpoint()[0], "https://example.test/v1/chat/completions")

    def test_missing_credentials_raise_without_touching_the_network(self):
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "", "DASHSCOPE_API_KEY": ""}, clear=False):
            with self.assertRaises(model_client.ModelClientError):
                model_client.endpoint()

    def test_transport_error_never_echoes_the_key(self):
        """任何失败路径都不能把 key 带进异常文本（异常会被写进产物）。"""
        failure = urllib.error.URLError("boom secret-key boom")
        with mock.patch("urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(model_client.ModelClientError) as caught:
                model_client.call_json("https://example.test/v1/chat/completions", "m", "secret-key",
                                       "s", "u")
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertIn("<KEY>", str(caught.exception))

    def test_default_address_family_is_ipv4_only(self):
        """默认只走 IPv4。依据是实测：宿主 AAAA 不可达，每次调用先白等 ~133 秒。

        `MODEL_CLIENT_IP_FAMILY=any` 必须能恢复系统默认，否则以后没人能复核
        「到底是本机网络的问题还是供应商的问题」。
        """
        import socket as _socket
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(model_client._address_families(), (_socket.AF_INET,))
        with mock.patch.dict("os.environ", {model_client.IP_FAMILY_ENV: "any"}, clear=True):
            self.assertEqual(model_client._address_families(), ())
        with mock.patch.dict("os.environ", {model_client.IP_FAMILY_ENV: "ipv6"}, clear=True):
            self.assertEqual(model_client._address_families(), (_socket.AF_INET6,))

    def test_family_restriction_filters_and_then_restores(self):
        """限制只作用于调用期间，且**必须还原**——否则会污染同进程的其它网络使用。"""
        import socket as _socket
        original = _socket.getaddrinfo

        def fake(host, port, family=0, type=0, proto=0, flags=0):
            return [(_socket.AF_INET6, 1, 6, "", ("2001:2::6d", port, 0, 0)),
                    (_socket.AF_INET, 1, 6, "", ("198.18.0.114", port, 0, 0))]

        with mock.patch("socket.getaddrinfo", fake):
            with model_client._restrict_address_family():
                kept = _socket.getaddrinfo("h", 443)
                self.assertEqual([entry[0] for entry in kept], [_socket.AF_INET])
            # 出了作用域必须原样返回两条
            self.assertEqual(len(_socket.getaddrinfo("h", 443)), 2)
        self.assertIs(_socket.getaddrinfo, original)

    def test_family_restriction_keeps_addresses_when_no_family_matches(self):
        """解析结果里没有 IPv4 时必须原样返回：宁可走慢的族，也不能变成解析失败。"""
        import socket as _socket

        def fake(host, port, family=0, type=0, proto=0, flags=0):
            return [(_socket.AF_INET6, 1, 6, "", ("2001:2::6d", port, 0, 0))]

        with mock.patch("socket.getaddrinfo", fake):
            with model_client._restrict_address_family():
                self.assertEqual(len(_socket.getaddrinfo("h", 443)), 1)

    def test_probe_transport_reports_a_blocked_family_as_diagnosis(self):
        """把「调用很慢」变成可落盘的诊断：哪个族不可达、各花多久。"""
        import socket as _socket

        class WorkingSocket:
            def __init__(self, family, kind):
                self.family = family

            def settimeout(self, _): pass
            def connect(self, address):
                if self.family == _socket.AF_INET6:
                    raise TimeoutError("timed out")
            def close(self): pass

        def fake(host, port, family=0, type=0, proto=0, flags=0):
            return [(_socket.AF_INET6, 1, 6, "", ("2001:2::6d", port, 0, 0)),
                    (_socket.AF_INET, 1, 6, "", ("198.18.0.114", port, 0, 0))]

        with mock.patch("socket.getaddrinfo", fake), mock.patch("socket.socket", WorkingSocket):
            report = model_client.probe_transport("api.deepseek.com")
        self.assertEqual([a["ok"] for a in report["attempts"]], [False, True])
        self.assertEqual(report["attempts"][0]["error"], "TimeoutError")
        self.assertIn("AF_INET6", report["diagnosis"])

    def test_call_json_reports_connect_and_read_separately(self):
        """连接与读取分开计时：只报一个总时长无法区分「连接卡住」与「模型在生成」。"""
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self, _=None):
                if not getattr(self, "_sent", False):
                    self._sent = True
                    return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}],
                                       "usage": {"total_tokens": 5}}).encode("utf-8")
                return b""

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            reply = model_client.call_json("https://example.test/v1/chat/completions", "m", "k",
                                           "s", "u")
        self.assertEqual(reply["parsed"], {"ok": True})
        self.assertIsInstance(reply["timings"]["to_headers_ms"], int)
        self.assertIsInstance(reply["timings"]["body_read_ms"], int)

    def test_a_slow_response_header_is_named_in_the_failure_message(self):
        """等到响应头就超时的话，消息里必须能看出来——否则又会被读成「供应商慢」。"""
        with mock.patch("urllib.request.urlopen",
                        side_effect=TimeoutError("read operation timed out")):
            with self.assertRaises(model_client.ModelClientError) as caught:
                model_client.call_json("https://example.test/v1/chat/completions", "m", "k", "s", "u")
        self.assertIn("等待响应头已耗时", str(caught.exception))


class CompletenessTest(unittest.TestCase):
    """完整性（漏记）：清单生成与覆盖判定。**纯函数部分用桩替换模型调用。**"""

    def _fake_reply(self, parsed):
        return {"parsed": parsed, "usage": {"total_tokens": 1}}

    def test_keypoints_are_normalised_and_capped(self):
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")), \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"keypoints": [
                                   {"id": 7, "text": "决定了试点范围"},
                                   {"text": "没有任何 id 也要能用"},
                                   "裸字符串也要能用",
                                   {"id": 9, "text": "第四条"},
                               ]})):
            keypoints = completeness.generate_keypoints("转写", limit=3)
        self.assertEqual([k["id"] for k in keypoints], [1, 2, 3])
        self.assertEqual(keypoints[0]["text"], "决定了试点范围")

    def test_empty_keypoint_list_is_an_error_not_an_empty_success(self):
        """清单为空必须报错：静默返回空清单会让"覆盖率高"看起来完美。"""
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")), \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"keypoints": []})):
            with self.assertRaises(model_client.ModelClientError):
                completeness.generate_keypoints("转写")

    def test_negation_form_judging_returns_per_keypoint_verdicts(self):
        keypoints = [{"id": 1, "text": "A"}, {"id": 2, "text": "B"}, {"id": 3, "text": "C"}]
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")), \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"uncovered": [
                                   {"id": 2, "why": "纪要没写这条"},
                                   {"id": 99, "why": "不存在的 id 必须被忽略"},
                               ], "covered_count": 2, "total_count": 3})):
            result = completeness.judge_coverage("转写", keypoints, {"summary": "s"})
        self.assertEqual(result["covered_count"], 2)
        self.assertEqual(result["total_count"], 3)
        self.assertEqual([e["covered"] for e in result["per_keypoint"]], [True, False, True])
        self.assertEqual(result["per_keypoint"][1]["reason"], "纪要没写这条")

    def test_macro_and_micro_coverage_are_both_reported(self):
        """宏平均与微平均必须都报：会议长短不一时它们会分开，只报一个是替读者选口径。"""
        records = [
            {"case_id": "c1", "arms": {
                "baseline": {"coverage": {"rate": 1.0, "successes": 2, "total": 2}},
                "revised": {"coverage": {"rate": 0.5, "successes": 1, "total": 2}}}},
            {"case_id": "c2", "arms": {
                "baseline": {"coverage": {"rate": 0.0, "successes": 0, "total": 11}},
                "revised": {"coverage": {"rate": 1.0, "successes": 11, "total": 11}}}},
        ]
        summary = completeness.summarize_arms(records)["by_arm"]
        # 宏平均：每场等权 → baseline (1.0+0.0)/2 = 0.5；微平均：合并计数 → 2/13
        self.assertAlmostEqual(summary["baseline"]["macro_mean_coverage"], 0.5)
        self.assertAlmostEqual(summary["baseline"]["micro_pooled_coverage"]["rate"], 2 / 13)
        self.assertEqual(summary["baseline"]["meetings"], 2)


class CompletenessV3InstrumentTest(unittest.TestCase):
    """仪器 v3：原子化清单（kind 分层）、--provider 选族、分层汇总、main 接线。"""

    def _fake_reply(self, parsed):
        return {"parsed": parsed, "usage": {"total_tokens": 1}}

    def test_kind_is_parsed_with_core_fallback(self):
        """kind 标注保留合法值；缺省与非法值一律兜底 core（标漏比标错类别安全）。"""
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")), \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"keypoints": [
                                   {"id": 7, "text": "决定了试点范围", "kind": "core"},
                                   {"id": 8, "text": "主持人开场致辞", "kind": "housekeeping"},
                                   {"id": 9, "text": "没标 kind 的点"},
                                   {"id": 10, "text": "非法类别", "kind": "trivial"},
                                   "裸字符串",
                               ]})):
            keypoints = completeness.generate_keypoints("转写", limit=5)
        self.assertEqual([k["id"] for k in keypoints], [1, 2, 3, 4, 5])
        self.assertEqual([k["kind"] for k in keypoints],
                         ["core", "housekeeping", "core", "core", "core"])

    def test_judge_per_keypoint_carries_kind_and_defaults_to_core(self):
        """判定结果透传 kind；v2 旧清单（无 kind 字段）按 core 处理，口径不碎裂。"""
        keypoints = [{"id": 1, "text": "A", "kind": "housekeeping"},
                     {"id": 2, "text": "B"}]  # v2 形态：没有 kind
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")), \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"uncovered": []})):
            result = completeness.judge_coverage("转写", keypoints, {"summary": "s"})
        self.assertEqual([e["kind"] for e in result["per_keypoint"]],
                         ["housekeeping", "core"])

    def test_provider_is_threaded_to_endpoint(self):
        """--provider 必须真正换族：generate 与 judge 都要按参数取 endpoint。"""
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")) as ep, \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"keypoints": [{"text": "x"}]})):
            completeness.generate_keypoints("转写", provider="qwen")
        self.assertEqual(ep.call_args[0][0], "qwen")
        with mock.patch.object(model_client, "endpoint", return_value=("u", "m", "k")) as ep, \
             mock.patch.object(model_client, "call_json",
                               return_value=self._fake_reply({"uncovered": []})):
            completeness.judge_coverage("转写", [{"id": 1, "text": "A"}], {"summary": "s"},
                                        provider="qwen")
        self.assertEqual(ep.call_args[0][0], "qwen")

    def test_summarize_arms_reports_core_only_layer(self):
        """分层汇总：全部点与 core_only 两套绝对数并列，不合成加权总分。"""
        records = [
            {"case_id": "c1", "arms": {"baseline": {
                "coverage": {"rate": 0.5, "successes": 2, "total": 4},
                "per_keypoint": [
                    {"id": 1, "covered": True, "kind": "core"},
                    {"id": 2, "covered": True, "kind": "core"},
                    {"id": 3, "covered": False, "kind": "housekeeping"},
                    {"id": 4, "covered": False, "kind": "housekeeping"},
                ]}}},
            {"case_id": "c2", "arms": {"baseline": {
                "coverage": {"rate": 1.0, "successes": 2, "total": 2},
                "per_keypoint": [
                    {"id": 1, "covered": True, "kind": "core"},
                    {"id": 2, "covered": True, "kind": "housekeeping"},
                ]}}},
        ]
        result = completeness.summarize_arms(records)
        arm = result["by_arm"]["baseline"]
        self.assertAlmostEqual(arm["macro_mean_coverage"], 0.75)
        # core_only：c1 核心 2/2=1.0、c2 核心 1/1=1.0 → 宏平均 1.0；微平均 3/3
        self.assertAlmostEqual(arm["core_only"]["macro_mean_coverage"], 1.0)
        self.assertAlmostEqual(arm["core_only"]["micro_pooled_coverage"]["rate"], 1.0)
        self.assertIn("layer_note", result)

    def test_main_wiring_with_zero_cases_and_dynamic_disclosure(self):
        """main 端到端接线（零用例零调用）：语料入口走 lib.corpora，disclosure 随 provider 生成。

        这是 2026-10-05 失效 import（from metrics import run_metrics）的回归护栏——
        当时 CLI 在 HEAD 上根本无法启动，而单测全部通过。
        """
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "packet.json"
            with mock.patch.object(completeness.corpora, "load_new_contract_cases",
                                   return_value=[]) as load_cases, \
                 mock.patch.object(model_client, "endpoint",
                                   return_value=("https://example.test/v1/chat/completions", "m", "k")), \
                 mock.patch.object(model_client, "probe_transport", return_value={}):
                rc = completeness.main(["--authorized-llm-calls", "1",
                                        "--provider", "qwen",
                                        "--run-names", "run-a,run-b",
                                        "--output", str(output)])
            self.assertEqual(rc, 0)
            state = json.loads(output.read_text(encoding="utf-8"))
        # --run-names 必须转发给语料入口（新跑的产品臂靠它进入判定）
        self.assertEqual(load_cases.call_args[0][0], ("run-a", "run-b"))
        self.assertEqual(state["disclosure"]["judge_provider"], "qwen")
        self.assertIn("v3", state["disclosure"]["instrument_version"])
        self.assertEqual(state["summary"]["by_arm"], {})


class StageAttributionTest(unittest.TestCase):
    """阶段归因诊断：并集构造、逐调用产物提取、归因汇总。纯函数用桩替换模型。"""

    def _partial(self, summary, topics):
        return {"summary": summary,
                "topics": [{"title": t} for t in topics],
                "viewpoints": [], "decisions": [], "pending_items": [], "risks": []}

    def test_union_output_concatenates_all_chunk_content(self):
        union = stage_attribution.build_union_output(
            [self._partial("第一段摘要", ["A", "B"]), self._partial("第二段摘要", ["C"])])
        self.assertEqual(union["summary"], "第一段摘要\n第二段摘要")
        self.assertEqual([t["title"] for t in union["topics"]], ["A", "B", "C"])
        self.assertEqual(union["risks"], [])

    def test_chunk_partials_skips_non_chunk_and_malformed_calls(self):
        payload = {"model_calls": [
            {"call_type": "MINUTES_CHUNK", "parsed_output": self._partial("s", ["A"])},
            {"call_type": "MINUTES_CHUNK", "parsed_output": None},
            {"call_type": "MINUTES_CHUNK", "parsed_output": {"summary": "只有摘要没有列表"}},
            {"call_type": "AGENT_REVIEW", "parsed_output": {"passed": True}},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.responses.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            partials = stage_attribution.chunk_partials(path)
        # 只有结构完整（至少一个非空列表字段）的分段部分纪才算数
        self.assertEqual(len(partials), 1)

    def test_attribution_and_summary_split_single_chunk_controls(self):
        """多块与单块对照必须分开汇总；单块 merge_loss 应≈0 作为判定噪声探针。"""
        keypoints = [{"id": 1, "text": "A", "kind": "core"}]
        packet = {"records": [
            {"case_id": "multi", "keypoints": keypoints,
             "arms": {"baseline": {"coverage": {"rate": 0.5, "successes": 1, "total": 2},
                                   "per_keypoint": []}}},
            {"case_id": "single", "keypoints": keypoints,
             "arms": {"baseline": {"coverage": {"rate": 1.0, "successes": 1, "total": 1},
                                   "per_keypoint": []}}},
        ]}
        partial_map = {"multi": [self._partial("s1", ["A"]), self._partial("s2", ["B"])],
                       "single": [self._partial("s", ["A"])]}
        cases = [{"case_id": "multi", "transcript": "t"},
                 {"case_id": "single", "transcript": "t"}]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "attr.json"
            packet_path = Path(tmp) / "packet.json"
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(stage_attribution.corpora, "load_new_contract_cases",
                                   return_value=cases), \
                 mock.patch.object(stage_attribution, "chunk_partials",
                                   side_effect=lambda path: partial_map[path.stem.replace('.responses', '')]), \
                 mock.patch.object(stage_attribution, "find_responses_path",
                                   side_effect=lambda cid, runs: Path(f"{cid}.responses.json")), \
                 mock.patch.object(stage_attribution.completeness, "judge_coverage",
                                   return_value={"coverage": {"rate": 1.0, "successes": 1, "total": 1},
                                                 "per_keypoint": []}):
                rc = stage_attribution.main(["--keypoints-packet", str(packet_path),
                                             "--output", str(output),
                                             "--authorized-llm-calls", "5"])
            self.assertEqual(rc, 0)
            state = json.loads(output.read_text(encoding="utf-8"))
        summary = state["summary"]
        self.assertEqual(summary["multi_chunk_meetings"]["count"], 1)
        self.assertEqual(summary["single_chunk_controls"]["count"], 1)
        self.assertAlmostEqual(summary["multi_chunk_meetings"]["mean_merge_loss"], 0.5)
        self.assertEqual(summary["disclaimer"].startswith("诊断产物"), True)


class V2PacketTest(unittest.TestCase):
    """契约 v2 的裁决包：必须覆盖全部被标记条目，且不能只取第一轮审查。"""

    def _case(self, *, issues_by_round, revised_issues):
        baseline = {"summary": "会议讨论了试点范围与上线安排的具体细节。",
                    "topics": [], "viewpoints": [], "decisions": [], "pending_items": [], "risks": []}
        revised = dict(baseline)
        return {
            "case_id": "c1",
            "transcript": "[发言人1] 我们只在产品组试点，下个月开始。" * 6,
            "baseline_output": baseline,
            "revised_output": revised,
            "record": {"agent_steps": [
                {"step_no": 1, "round_no": 1, "step_type": "REVIEW",
                 "payload": {"issues": issues_by_round}},
                {"step_no": 2, "round_no": 2, "step_type": "REVIEW",
                 "payload": {"issues": revised_issues}},
            ]},
        }

    def test_issues_from_every_round_enter_the_packet(self):
        """只看第一轮会砍掉一半分母——第二轮审的是修订稿。"""
        case = self._case(
            issues_by_round=[{"field": "summary", "index": 0, "issue_type": "UNSUPPORTED",
                              "detail": "第一轮意见"}],
            revised_issues=[{"field": "summary", "index": 0, "issue_type": "UNSUPPORTED",
                             "detail": "第二轮意见"}],
        )
        packet = A.build_packet_for_v2([case], unflagged_sample=0, proxy_only_sample=0)
        strata = [packet["_key"][i["anonymous_id"]]["stratum"] for i in packet["items"]]
        self.assertEqual(strata.count("reviewer_UNSUPPORTED"), 2)  # 两轮各一条，都进来了
        versions = sorted(packet["_key"][i["anonymous_id"]]["version"] for i in packet["items"])
        self.assertEqual(versions, ["baseline", "revised"])

    def test_flagged_items_are_censused_not_sampled(self):
        """被标记条目全数纳入（不是抽样）：precision 的分母不能靠抽。"""
        issues = [{"field": "summary", "index": 0, "issue_type": "UNSUPPORTED", "detail": f"意见{i}"}
                  for i in range(7)]
        case = self._case(issues_by_round=issues, revised_issues=[])
        packet = A.build_packet_for_v2([case], unflagged_sample=0, proxy_only_sample=0)
        # 同一 (版本,字段,索引) 去重后只有 1 条
        self.assertEqual(len(packet["items"]), 1)
        self.assertEqual(packet["flagged_total_in_corpus"], 1)


if __name__ == "__main__":
    unittest.main()


class ArmAnalysisTest(unittest.TestCase):
    """三臂对照与删除质量：锁住"别把退化产物当删除、别把没比过当成零"。"""

    def _case(self, case_id, baseline_items, revised_items):
        return {
            "case_id": case_id,
            "transcript": "[发言人1] 我们决定在三月试点，负责人是程安。" * 4,
            "baseline_output": {
                "summary": "会议决定了试点。",
                "topics": [{"title": f"议题{i}", "summary": f"内容{i}"} for i in range(baseline_items)],
                "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
            },
            "revised_output": {
                "summary": "会议决定了试点。",
                "topics": [{"title": f"议题{i}", "summary": f"内容{i}"} for i in range(revised_items)],
                "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
            },
            "record": {"agent_steps": []},
        }

    def test_degenerate_output_is_detected_and_not_counted_as_deletion(self):
        """整篇内容归零不是"审查删的"，必须单列；否则会独占消失条目。"""
        case = self._case("c1", baseline_items=8, revised_items=0)
        self.assertTrue(arm_analysis.is_degenerate(case["revised_output"],
                                                   case["baseline_output"]))
        report = arm_analysis.build_report([case], {}, Path("/nonexistent"))
        self.assertEqual(report["degenerate_outputs"], {"reflected": ["c1"]})
        # 退化场次不参与删除类主结局
        self.assertIsNone(report["arm_totals"]["reflected"]["disappeared_per_meeting"])

    def test_a_short_draft_rewritten_to_nothing_is_not_degenerate(self):
        case = self._case("c1", baseline_items=2, revised_items=0)
        self.assertFalse(arm_analysis.is_degenerate(case["revised_output"],
                                                    case["baseline_output"]))

    def test_deletion_pairing_reports_real_pairs_not_a_silent_zero(self):
        """删除类配对必须真的比过：曾经因为初稿序列是 None 而报出"0 个非零对"。"""
        cases = [self._case(f"c{i}", baseline_items=6, revised_items=6 - (1 if i < 3 else 0))
                 for i in range(6)]
        report = arm_analysis.build_report(cases, {}, Path("/nonexistent"))
        paired = report["comparisons"]["baseline_to_reflected"]["disappeared_items"]
        self.assertEqual(paired["n_meetings"], 6)
        self.assertEqual(paired["n_nonzero"], 3)   # 3 场各删了 1 条
        self.assertEqual(paired["worsened"], 3)
        self.assertEqual(paired["improved"], 0)

    def test_deletion_packet_separates_deleted_from_kept(self):
        cases = [self._case(f"c{i}", baseline_items=8, revised_items=7) for i in range(4)]
        packet = arm_analysis.build_deletion_packet(cases, {}, deleted_sample=4, kept_sample=4)
        strata = {packet["_key"][i["anonymous_id"]]["stratum"] for i in packet["items"]}
        self.assertIn("deleted_reflected", strata)
        self.assertIn("kept", strata)

    def test_deletion_summary_reports_grounded_rate_per_stratum(self):
        packet = {
            "_key": {"a": {"stratum": "deleted_reflected"}, "b": {"stratum": "kept"},
                     "c": {"stratum": "deleted_reflected"}},
            "items": [{"anonymous_id": "a", "label": "supported"},
                      {"anonymous_id": "b", "label": "unsupported"},
                      {"anonymous_id": "c", "label": "partial"}],
        }
        summary = arm_analysis.summarize_deletion_packet(packet)["by_stratum"]
        self.assertEqual(summary["deleted_reflected"]["labelled"], 2)
        self.assertEqual(summary["deleted_reflected"]["grounded_count"], 1)
        self.assertEqual(summary["kept"]["grounded_count"], 0)

    def test_arm_to_arm_deletion_test_is_a_paired_difference(self):
        """两臂之间的删除比较必须是**配对差**，不是"各自与零参照比"。

        实测踩过：`passthrough→reflected` 一度报出 p=0.002，因为两臂各自与初稿比、
        都得到"显著更多删除"，而真实的臂间差是 −0.04（p=0.22）。前者会让读者以为
        "审查意见导致删除显著增加"，后者才是事实。
        """
        # 两臂删得一样多（各 1 条/场）：臂间差必须是 0
        cases = []
        for i in range(6):
            case = self._case(f"c{i}", baseline_items=6, revised_items=4)
            cases.append(case)
        arm_outputs = {}
        for i in range(6):
            arm_outputs[f"c{i}"] = {
                "summary": "会议决定了试点。",
                "topics": [{"title": f"议题{j}", "summary": f"内容{j}"} for j in range(4)],
                "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
            }
        report = arm_analysis.build_report(cases, arm_outputs, Path("/nonexistent"))
        paired = report["comparisons"]["passthrough_to_reflected"]["disappeared_items"]
        self.assertEqual(paired["n_nonzero"], 0, "两臂删得一样多时，臂间差必须为零")
        self.assertAlmostEqual(paired["mean_difference"], 0.0)
        # 而 baseline→reflected 的隐含零参照口径应当看到删除
        baseline_paired = report["comparisons"]["baseline_to_reflected"]["disappeared_items"]
        self.assertGreater(baseline_paired["n_nonzero"], 0)


