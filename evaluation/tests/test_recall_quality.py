"""可检索事实召回（recall-v2）与质量闸仪器的零调用测试。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
GOLD = EVAL / "gold"
RESULTS = EVAL / "results"

if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from lib import recall_quality  # noqa: E402

KNOWN_CLASSES = {"retrievable", "metadata", "fixture", "conflict", "judge_uncertain"}


def _packet(facts_per_case, found_per_case, judgements=None):
    cases = []
    for index, (facts, found) in enumerate(zip(facts_per_case, found_per_case, strict=True)):
        judgement = {"context": {"facts": [
            {"index": position + 1, "found": value} for position, value in enumerate(found)]}}
        case = {"case_id": f"c{index}", "expected_facts": facts,
                "judgement": judgement}
        if judgements:
            case = judgements[index]
        cases.append(case)
    return {"cases": cases}


class RecallQualityTest(unittest.TestCase):
    def test_metadata_excluded_from_denominator(self):
        manifest = {"human_checked": False, "sets": {"s": {
            "default": "retrievable",
            "cases": {"c0": {"1": "metadata"}},
        }}}
        packet = _packet([["a", "b", "c"], ["x", "y"]],
                         [[True, False, True], [True, False]])
        measured = recall_quality.retrievable_recall(packet, "s", manifest)
        # c0: 3 facts, 1 metadata excluded -> 2 counted (1 found); c1: 2 counted (1 found)
        self.assertEqual(measured["total"], 4)
        self.assertEqual(measured["successes"], 3)
        self.assertEqual(measured["excluded"], 1)
        self.assertAlmostEqual(measured["rate"], 0.75)

    def test_judgement_fallback_to_cross_family_label(self):
        packet = {"cases": [{
            "case_id": "c0", "expected_facts": ["a", "b"],
            "judgements": {"f": {"context": {"facts": [
                {"index": 1, "found": True}, {"index": 2, "found": True}]}}},
        }]}
        manifest = {"sets": {"s": {"default": "retrievable", "cases": {}}}}
        measured = recall_quality.retrievable_recall(packet, "s", manifest)
        self.assertEqual((measured["successes"], measured["total"]), (2, 2))

    def test_unknown_set_reports_unavailable(self):
        measured = recall_quality.retrievable_recall({"cases": []}, "nope", {"sets": {}})
        self.assertIsNone(measured["rate"])
        self.assertIn("清单里没有", measured.get("note", ""))

    def test_manifest_matches_gold_and_known_classes(self):
        manifest = recall_quality.load_manifest()
        self.assertTrue(manifest.get("sets"), "fact_classes.json 缺失或为空")
        gold_of = {
            "cross_30": "cross_meeting_qa_30.json",
            "new_meeting_15": "new_meeting_15.json",
            "eval_100_mixed": "eval_100_mixed.json",
        }
        for set_name, rel in gold_of.items():
            data = json.loads((GOLD / rel).read_text(encoding="utf-8"))
            facts = {str(case.get("case_id")): case.get("expected_facts") or []
                     for case in data.get("cases") or []}
            payload = manifest["sets"].get(set_name)
            self.assertIsNotNone(payload, f"{set_name} 不在清单里")
            for case_id, classes in (payload.get("cases") or {}).items():
                self.assertIn(case_id, facts, f"{set_name}/{case_id} 不在 gold")
                for index_text, klass in classes.items():
                    self.assertIn(klass, KNOWN_CLASSES)
                    self.assertLess(int(index_text), len(facts[case_id]),
                                    f"{set_name}/{case_id}#{index_text} 越界")

    def test_cross30_metadata_count_is_eight(self):
        """cross30 的 11 条单审计 metadata 里有 3 条（cs06#0-2）被同题 packet 反证为可检索，
        更正后 metadata 为 8 条（独立验证 round2 §2.4）。"""
        manifest = recall_quality.load_manifest()
        classes = [klass for case in manifest["sets"]["cross_30"]["cases"].values()
                   for klass in case.values()]
        self.assertEqual(classes.count("metadata"), 8)

    def test_frozen_recall_v2_rates(self):
        """当前产物的 recall-v2 读数锁定：旧口径 75.86% → 可检索口径 88.00%。"""
        manifest = recall_quality.load_manifest()
        packet = json.loads((RESULTS / "cross30_fresh_packet.json").read_text(encoding="utf-8"))
        measured = recall_quality.retrievable_recall(packet, "cross_30", manifest)
        legacy = recall_quality.legacy_recall(packet)
        self.assertEqual((measured["successes"], measured["total"]), (44, 50))
        self.assertAlmostEqual(measured["rate"], 44 / 50)
        self.assertAlmostEqual(legacy["rate"], 44 / 58)


if __name__ == "__main__":
    unittest.main()
