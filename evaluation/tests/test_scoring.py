import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.scoring import (
    _load_manifest_references, _load_records, character_rouge,
    format_summary_markdown, score_records,
)


HERE = Path(__file__).resolve().parents[1] / "benchmarks"


def record(case_id="one", baseline="甲", revised="甲乙", status="completed"):
    return {
        "case_id": case_id,
        "status": status,
        "baseline": {"output": {"summary": baseline}},
        "revised": {"output": {"summary": revised}, "revision_generated": baseline != revised},
        "telemetry": {"total_tokens": 100, "llm_elapsed_ms_sum": 2000},
    }


class CharacterRougeTests(unittest.TestCase):
    def test_rouge_uses_character_counts_and_order_for_lcs(self):
        result = character_rouge("甲甲，乙", "甲乙甲")
        self.assertEqual(result["rouge_1"]["f1"], 1.0)
        self.assertAlmostEqual(result["rouge_2"]["f1"], 0.5)
        self.assertAlmostEqual(result["rouge_l"]["f1"], 2 / 3)

    def test_empty_prediction_scores_zero(self):
        result = character_rouge("", "甲乙")
        self.assertTrue(all(result[name]["f1"] == 0 for name in result))

    def test_empty_reference_is_invalid(self):
        with self.assertRaises(ValueError):
            character_rouge("甲", " ！")


class PairedScoringTests(unittest.TestCase):
    def test_scores_paired_delta_and_reports_failed_exclusion(self):
        report = score_records(
            [record(), record("two", status="failed")],
            {"one": "甲乙", "two": "甲乙"},
        )
        self.assertEqual(report["paired_case_count"], 1)
        self.assertEqual(report["excluded"][0]["case_id"], "two")
        self.assertAlmostEqual(report["aggregate"]["rouge_1"]["paired_mean_delta_f1"], 1 / 3)
        self.assertEqual(report["aggregate"]["rouge_2"]["improved_count"], 1)
        self.assertEqual(report["telemetry"]["total_tokens"]["sum"], 200)
        self.assertEqual(report["scored_telemetry"]["total_tokens"]["sum"], 100)
        self.assertIsNone(report["aggregate"]["rouge_1"]["paired_mean_delta_f1_bootstrap_95ci"])
        self.assertNotIn("甲乙", str(report))  # References stay in the locked manifest.

    def test_duplicate_and_missing_references_fail_loudly(self):
        with self.assertRaisesRegex(ValueError, "duplicate case_id"):
            score_records([record(), record()], {"one": "甲乙"})
        with self.assertRaisesRegex(ValueError, "no official reference"):
            score_records([record()], {})

    def test_two_meetings_bootstrap_paired_delta(self):
        report = score_records(
            [record(), record("two", baseline="甲乙", revised="甲")],
            {"one": "甲乙", "two": "甲乙"},
        )
        metric = report["aggregate"]["rouge_1"]
        self.assertAlmostEqual(metric["paired_mean_delta_f1"], 0.0)
        self.assertEqual(metric["improved_count"], 1)
        self.assertEqual(metric["worsened_count"], 1)
        self.assertEqual(len(metric["paired_mean_delta_f1_bootstrap_95ci"]), 2)

    def test_interrupted_run_reports_unseen_references_and_attempted_cost(self):
        report = score_records(
            [record("one"), record("two", status="failed")],
            {"one": "甲乙", "two": "甲乙", "three": "甲乙"},
        )
        self.assertEqual(report["reference_case_count"], 3)
        self.assertEqual(report["result_case_count"], 2)
        self.assertEqual(report["paired_case_count"], 1)
        self.assertEqual(report["missing_case_ids"], ["three"])
        self.assertEqual(report["telemetry"]["total_tokens"]["sum"], 200)
        markdown = format_summary_markdown(report)
        self.assertIn("1/3 场", markdown)
        self.assertIn("three", markdown)
        self.assertIn("包含失败样例", markdown)
        self.assertIn("负责人、期限", markdown)


class OfficialManifestTests(unittest.TestCase):
    def test_pinned_dev_and_repository_test_manifests_are_accepted(self):
        self.assertEqual(len(_load_manifest_references(HERE / "manifest.json")), 3)
        self.assertEqual(len(_load_manifest_references(HERE / "manifest_all_dev.json")), 22)
        self.assertEqual(len(_load_manifest_references(HERE / "manifest_test_chunk6000.json")), 26)

    def test_repository_test_split_source_and_record_hashes_are_checked(self):
        original = json.loads((HERE / "manifest_test_chunk6000.json").read_text(encoding="utf-8"))
        changes = (
            (lambda m: m["source"].__setitem__("split", "dev"), "source repository or split"),
            (lambda m: m["source"].__setitem__("repository_url", "https://example.invalid/VCSum"), "source repository or split"),
            (lambda m: m["cases"][0].__setitem__("split", "dev"), "case split differs"),
            (lambda m: m["cases"][0].__setitem__("reference_overall", "tampered"), "reference checksum mismatch"),
            (lambda m: m["cases"][0].__setitem__("transcript", "tampered"), "transcript checksum mismatch"),
        )
        with tempfile.TemporaryDirectory(dir=HERE) as temporary:
            path = Path(temporary) / "manifest.json"
            for change, error in changes:
                with self.subTest(error=error):
                    manifest = json.loads(json.dumps(original, ensure_ascii=False))
                    change(manifest)
                    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        _load_manifest_references(path)

    def test_saved_report_must_match_manifest_hash_when_provided(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps({"manifest_sha256": "0" * 64, "records": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest_sha256 differs"):
                _load_records(path, manifest_sha256="1" * 64)
            self.assertEqual(_load_records(path, manifest_sha256="0" * 64), [])


if __name__ == "__main__":
    unittest.main()
