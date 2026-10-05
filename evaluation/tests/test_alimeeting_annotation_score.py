"""Offline scoring tests: complete human labels, report lock, and count units."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(HERE.parents[1]))
from benchmarks import alimeeting_annotation_score as scorer  # noqa: E402
from benchmarks import alimeeting_review as review  # noqa: E402
from benchmarks.alimeeting_adapter import load_cases  # noqa: E402
from tests.test_alimeeting_adapter import (  # noqa: E402
    example_record, write_sources,
)


def _minutes(*names: str) -> dict:
    return {
        "summary": "summary", "topics": [], "viewpoints": [],
        "decisions": [{"content": name, "basis": "basis", "owner_suggestion": "owner",
                       "deadline_suggestion": ""} for name in names if "decision" in name],
        "pending_items": [{"content": name, "owner_suggestion": "owner",
                           "deadline_suggestion": ""} for name in names if "pending" in name],
        "risks": [],
    }


class AnnotationScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        write_sources(self.source,
                      [example_record("M0001", action_ids=[2]),
                       example_record("M0002", action_ids=[])], [])
        self.cases = load_cases(self.source, splits=("dev",))
        self.report_path = self.root / "report.json"
        self.review_dir = self.root / "review"
        self.key_path = self.root / "stage_key.json"
        self.report = {
            "mode": "live", "dataset_name": "AliMeeting4MUG", "dataset_id": review.DATASET_ID,
            "synthetic": False, "asr_bypassed": True, "status": "completed",
            "plan": {"cases": [{"case_id": case["id"]} for case in self.cases]},
            "records": [],
        }
        for case in self.cases:
            positive = case["id"].endswith("M0001")
            self.report["records"].append({
                "case_id": case["id"], "status": "completed",
                "source": {"case_id": case["id"], "split": "dev",
                           "transcript_sha256": case["transcript_sha256"],
                           "input_char_count": len(case["transcript"])},
                "baseline": {"output": _minutes("good_decision_base", "bad_pending_base")
                             if positive else _minutes("false_pending_base")},
                "revised": {"output": _minutes("good_decision_revised")
                            if positive else _minutes("nonaction_decision_revised")},
            })
        locked = {"dataset_id": review.DATASET_ID, "synthetic": False,
                  "cases": self.cases}
        raw = json.dumps(locked, ensure_ascii=False).encode("utf-8")
        (self.root / "manifest.lock.json").write_bytes(raw)
        manifest_hash = hashlib.sha256(raw).hexdigest()
        self.report["manifest_sha256"] = manifest_hash
        for record in self.report["records"]:
            record["source"]["manifest_sha256"] = manifest_hash
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False), encoding="utf-8")
        review.prepare(self.report_path, self.source, self.review_dir, self.key_path, seed=4)
        self.items_path = self.review_dir / "review_items.csv"

    def _rows(self) -> list[dict[str, str]]:
        with self.items_path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def _write_rows(self, rows: list[dict[str, str]]) -> None:
        with self.items_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=review.ITEM_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _annotate(self) -> list[dict[str, str]]:
        rows = self._rows()
        for row in rows:
            content = row["content"]
            good = content.startswith("good_")
            nonaction = content.startswith("nonaction_")
            overstated = content.startswith("bad_")
            row.update({"evidence_sentence_ids": "2" if good else "1" if nonaction or overstated else "",
                        "actionable": "0" if nonaction else "1",
                        "grounded": "1" if good or nonaction or overstated else "0",
                        "overstated": "1" if overstated else "0", "duplicate": "0"})
        self._write_rows(rows)
        return rows

    def _score(self) -> dict:
        return scorer.score(self.report_path, self.source, self.items_path, self.key_path)

    def test_rates_keep_nonaction_unanchored_items_and_zero_action_stage(self):
        self._annotate()
        report = self._score()
        baseline = report["overall"]["baseline"]
        revised = report["overall"]["revised"]
        self.assertEqual(baseline["generated_items"], 3)
        self.assertEqual(revised["generated_items"], 2)
        self.assertEqual(revised["nonaction_items"], 1)
        self.assertEqual(revised["nonaction_item_rate"], .5)
        self.assertEqual(baseline["grounded_correct_actionable_rate"], 1 / 3)
        self.assertEqual(revised["grounded_correct_actionable_rate"], 1)
        self.assertEqual(baseline["ungrounded_item_rate"], 1 / 3)
        self.assertEqual(baseline["unanchored_actionable_items"], 1)
        self.assertEqual(baseline["anchor_non_gold_cited_sentences"], 1)
        self.assertEqual(baseline["anchor_precision"], .5)
        self.assertEqual(baseline["anchor_recall"], 1)
        self.assertAlmostEqual(baseline["anchor_f1"], 2 / 3)
        self.assertEqual(baseline["gold_positive_sentence_coverage"], 1)
        self.assertEqual(baseline["action_output_rate_on_zero_gold_meetings"], 1)
        self.assertEqual(revised["action_output_rate_on_zero_gold_meetings"], 0)
        self.assertEqual(report["cases"]["alimug_M0002"]["revised"]["actionable_items"], 0)
        self.assertAlmostEqual(report["overall"]["paired_delta_revised_minus_baseline"]
                               ["grounded_correct_actionable_rate"], 2 / 3)
        self.assertFalse(report["overall"]["bootstrap"]["available"])
        self.assertFalse(report["official_aid_leaderboard_metric"])

    def test_incomplete_annotation_refuses_to_load_gold(self):
        original_loader = scorer.load_action_labels
        try:
            def forbidden(*args, **kwargs):
                raise AssertionError("gold loaded before annotation validation")
            scorer.load_action_labels = forbidden
            with self.assertRaisesRegex(ValueError, "must be filled"):
                self._score()
        finally:
            scorer.load_action_labels = original_loader

    def test_missing_duplicate_or_mutated_generated_item_is_rejected(self):
        rows = self._annotate()
        for mutation, expected_message in (
            (lambda copied: copied.pop(), "omits"),
            (lambda copied: copied.append(copied[0].copy()), "Duplicate item_id"),
            (lambda copied: copied[0].update(content="edited output"), "content was changed"),
            (lambda copied: copied[0].update(item_id="fake"), "Unexpected item_id"),
        ):
            copied = [row.copy() for row in rows]
            mutation(copied)
            self._write_rows(copied)
            with self.subTest(expected_message=expected_message), self.assertRaisesRegex(
                    ValueError, expected_message):
                self._score()

    def test_evidence_and_boolean_validation(self):
        rows = self._annotate()
        good_index = next(i for i, row in enumerate(rows) if row["content"].startswith("good_"))
        for field, value, message in (
            ("evidence_sentence_ids", "999", "absent from its meeting"),
            ("evidence_sentence_ids", "2;2", "duplicate evidence"),
            ("evidence_sentence_ids", "2,1", "semicolon-separated"),
            ("evidence_sentence_ids", "", "grounded=1 requires"),
            ("actionable", "yes", "must be filled with 0 or 1"),
        ):
            copied = [row.copy() for row in rows]
            copied[good_index][field] = value
            self._write_rows(copied)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, message):
                self._score()

    def test_ungrounded_actionable_with_citation_does_not_gain_anchor_credit(self):
        rows = self._annotate()
        original = self._score()["overall"]["baseline"]
        target = next(row for row in rows if row["content"] == "false_pending_base")
        target["evidence_sentence_ids"] = "1"
        self._write_rows(rows)
        changed = self._score()["overall"]["baseline"]
        self.assertEqual(changed["excluded_ungrounded_actionable_items_with_evidence"], 1)
        for metric in ("anchor_predicted_sentences", "anchor_precision", "anchor_recall",
                       "anchor_f1", "gold_positive_sentence_coverage"):
            self.assertEqual(changed[metric], original[metric])

    def test_empty_generated_stage_is_a_meeting_with_no_action_output(self):
        empty = scorer._aggregate([scorer._case_counts([], set())])
        self.assertEqual(empty["meetings"], 1)
        self.assertEqual(empty["generated_items"], 0)
        self.assertEqual(empty["actionable_items"], 0)
        self.assertIsNone(empty["grounded_correct_actionable_rate"])
        self.assertEqual(empty["action_output_rate_on_zero_gold_meetings"], 0)

    def test_stage_key_and_report_digest_must_match(self):
        self._annotate()
        key = json.loads(self.key_path.read_text(encoding="utf-8"))
        key["case_stage_map"]["alimug_M0001"] = {"A": "baseline", "B": "baseline"}
        self.key_path.write_text(json.dumps(key), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mapping"):
            self._score()
        key["case_stage_map"] = review._stage_map([case["id"] for case in self.cases], 4)
        self.key_path.write_text(json.dumps(key), encoding="utf-8")
        self.report_path.write_text(self.report_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exact live report bytes"):
            self._score()

    def test_bootstrap_resamples_meetings_as_pairs(self):
        bad = {"actionable": True, "grounded": False,
               "overstated": False, "duplicate": False, "evidence": set()}
        good = {"actionable": True, "grounded": True,
                "overstated": False, "duplicate": False, "evidence": {"2"}}
        counts = {f"case-{i}": {"baseline": scorer._case_counts([bad], {"2"}),
                                "revised": scorer._case_counts([good], {"2"})}
                  for i in range(10)}
        bootstrap = scorer._bootstrap_paired(counts, draws=200)
        self.assertTrue(bootstrap["available"])
        ci = bootstrap["paired_delta_95_percentile_ci"]["action_output_rate_on_zero_gold_meetings"]
        self.assertIsNone(ci)
        for name in ("grounded_correct_actionable_rate", "gold_positive_sentence_coverage", "anchor_f1"):
            self.assertEqual(bootstrap["paired_delta_95_percentile_ci"][name],
                             {"lower": 1, "upper": 1, "valid_draws": 200})


if __name__ == "__main__":
    unittest.main()
