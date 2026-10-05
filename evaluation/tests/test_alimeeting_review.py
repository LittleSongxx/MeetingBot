"""Offline checks for source-locked, gold-blind AliMeeting review preparation."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(HERE.parents[1]))
from benchmarks import alimeeting_review as review  # noqa: E402
from benchmarks.alimeeting_adapter import load_cases  # noqa: E402


def _output(label: str) -> dict:
    return {
        "summary": "not reviewer-visible", "topics": [], "viewpoints": [],
        "decisions": [{"content": f"{label} 决策", "basis": f"{label} 依据",
                       "owner_suggestion": "负责人", "deadline_suggestion": "2026-10-01"}],
        "pending_items": [{"content": f"{label} 待办", "owner_suggestion": "待确认",
                           "deadline_suggestion": ""}],
        "risks": [],
    }


class AliMeetingReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(splits=("dev",))[:2]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report_path = self.root / "report.json"
        self.review_dir = self.root / "reviewer_copy"
        self.key_path = self.root / "stage_key.json"
        self.report = {
            "mode": "live", "dataset_name": "AliMeeting4MUG", "dataset_id": review.DATASET_ID,
            "synthetic": False, "asr_bypassed": True, "status": "completed",
            "plan": {"cases": [{"case_id": case["id"]} for case in self.cases]},
            "records": [{
                "case_id": case["id"], "status": "completed",
                "source": {"case_id": case["id"], "split": case["split"],
                           "transcript_sha256": case["transcript_sha256"],
                           "input_char_count": len(case["transcript"])},
                "baseline": {"output": _output("初稿")},
                "revised": {"output": _output("修订稿")},
                "action_ids": ["GOLD_SENTINEL_MUST_NOT_LEAK"],
            } for case in self.cases],
        }
        self.locked = {"dataset_id": review.DATASET_ID, "synthetic": False,
                       "cases": [{**case, "action_ids": "GOLD_SENTINEL_MUST_NOT_LEAK"}
                                 for case in self.cases]}
        self._write_fixture()

    def _write_fixture(self):
        raw = json.dumps(self.locked, ensure_ascii=False).encode("utf-8")
        (self.root / "manifest.lock.json").write_bytes(raw)
        manifest_hash = hashlib.sha256(raw).hexdigest()
        self.report["manifest_sha256"] = manifest_hash
        for record in self.report["records"]:
            record["source"]["manifest_sha256"] = manifest_hash
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False), encoding="utf-8")

    def test_blind_sheets_preserve_original_sentences_and_hide_gold(self):
        result = review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir,
                                self.key_path, seed=42)
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["item_count"], 8)
        self.assertEqual(result["model_calls"], 0)
        with (self.review_dir / "source_sentences.csv").open(encoding="utf-8", newline="") as stream:
            sentences = list(csv.DictReader(stream))
        with (self.review_dir / "review_items.csv").open(encoding="utf-8", newline="") as stream:
            items = list(csv.DictReader(stream))
        self.assertEqual(len(sentences), sum(len(case["segments"]) for case in self.cases))
        first_source = next(row for row in sentences if row["case_id"] == self.cases[0]["id"])
        self.assertEqual(first_source["text"], self.cases[0]["segments"][0]["text"])
        self.assertEqual(first_source["sentence_id"], str(self.cases[0]["segments"][0]["source_sentence_id"]))
        self.assertEqual({row["stage"] for row in items}, {"A", "B"})
        self.assertEqual(len({row["item_id"] for row in items}), len(items))
        self.assertTrue(all(all(row[name] == "" for name in review.ANNOTATION_FIELDS) for row in items))
        self.assertEqual(set(items[0]), set(review.ITEM_FIELDS))
        self.assertEqual({row["item_kind"] for row in items}, {"decision", "pending_item"})
        reviewer_text = "".join(path.read_text(encoding="utf-8") for path in self.review_dir.iterdir())
        self.assertNotIn("GOLD_SENTINEL_MUST_NOT_LEAK", reviewer_text)
        self.assertNotIn("action_ids", reviewer_text)
        self.assertNotIn('"baseline"', reviewer_text)
        self.assertNotIn('"revised"', reviewer_text)
        self.assertNotIn("not reviewer-visible", reviewer_text)
        key = json.loads(self.key_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        for case in self.cases:
            self.assertEqual(set(key["case_stage_map"][case["id"]].values()), {"baseline", "revised"})
            self.assertEqual(key["source_case_hashes"][case["id"]]["transcript_sha256"],
                             case["transcript_sha256"])

    def test_randomization_repeats_for_same_seed_even_if_record_order_changes(self):
        review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir, self.key_path, seed=19)
        first = json.loads(self.key_path.read_text())["case_stage_map"]
        self.report["records"].reverse()
        self._write_fixture()
        another = self.root / "reviewer_copy_2"
        another_key = self.root / "stage_key_2.json"
        review.prepare(self.report_path, review.SOURCE_DIR, another, another_key, seed=19)
        second = json.loads(another_key.read_text())["case_stage_map"]
        self.assertEqual(first, second)
        self.assertEqual(sum(value["A"] == "baseline" for value in first.values()), 1)

    def test_single_case_pilot_can_receive_either_blind_letter(self):
        case_id = self.cases[0]["id"]
        observed = {review._stage_map([case_id], seed)[case_id]["A"] for seed in range(16)}
        self.assertEqual(observed, {"baseline", "revised"})

    def test_mismatched_report_or_locked_source_hash_fails_before_writing(self):
        self.report["records"][0]["source"]["transcript_sha256"] = "0" * 64
        self._write_fixture()
        with self.assertRaisesRegex(ValueError, "source identity/hash"):
            review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir, self.key_path)
        self.assertFalse(self.review_dir.exists())
        self.assertFalse(self.key_path.exists())

        self.report["records"][0]["source"]["transcript_sha256"] = self.cases[0]["transcript_sha256"]
        self.locked["cases"][0]["source_record_sha256"] = "0" * 64
        self._write_fixture()
        with self.assertRaisesRegex(ValueError, "locked source_record_sha256"):
            review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir, self.key_path)

    def test_partial_report_or_key_in_reviewer_dir_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the reviewer-visible"):
            review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir,
                           self.review_dir / "stage_key.json")
        self.report["status"] = "running"
        self._write_fixture()
        with self.assertRaisesRegex(ValueError, "completed"):
            review.prepare(self.report_path, review.SOURCE_DIR, self.review_dir, self.key_path)
        self.assertFalse(self.review_dir.exists())


if __name__ == "__main__":
    unittest.main()
