"""Tests for the label-isolated AliMeeting4MUG source adapter."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from benchmarks import alimeeting_adapter as adapter


def example_record(key: str, *, action_ids: list[int] | None = None) -> dict:
    return {
        "meeting_key": key,
        "sentences": [
            {"id": 1, "speaker": "no.0", "start_time": "1.234", "end_time": "2.501",
             "s": "安排上线检查。"},
            {"id": 2, "speaker": "no.1", "start_time": "2.500", "end_time": "3.250",
             "s": "小王下周完成检查。"},
        ],
        "action_ids": [{"id": value} for value in (action_ids or [])],
    }


def write_sources(root: Path, dev: list[dict], test: list[dict]) -> None:
    archives = {}
    for filename, records in (("dev.zip", dev), ("except_TS_test1.zip", test)):
        member = filename.removesuffix(".zip") + ".csv"
        rows = ["idx\tcontent"]
        for index, record in enumerate(records):
            rows.append(f"{index}\t{json.dumps(record, ensure_ascii=False)}")
        with zipfile.ZipFile(root / filename, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(member, "\n".join(rows) + "\n")
        raw = (root / filename).read_bytes()
        archives[filename] = {"path": filename, "bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest(), "zip_members": [member]}
    source = {"dataset_id": adapter.DATASET_ID,
              "source_url": "https://modelscope.cn/datasets/modelscope/Alimeeting4MUG/summary",
              "metadata_git_revision": "a" * 40, "archives": archives}
    (root / "source-manifest.json").write_text(json.dumps(source), encoding="utf-8")


class AliMeetingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_sources(self.root, [example_record("M0001", action_ids=[2])],
                      [example_record("M0002")])

    def test_real_milliseconds_mapping_and_separate_gold(self) -> None:
        manifest = adapter.build_manifest(self.root)
        self.assertEqual(manifest["selection"]["split_counts"], {"dev": 1, "test": 1})
        case = manifest["cases"][0]
        self.assertEqual(case["id"], "alimug_M0001")
        self.assertEqual(case["segments"][0]["start_ms"], 1234)
        self.assertEqual(case["segments"][0]["end_ms"], 2501)
        self.assertEqual(case["segments"][1]["start_ms"], 2500)
        self.assertEqual(case["sentence_id_to_segment_index"], {"1": 0, "2": 1})
        self.assertFalse(case["timestamps_synthetic_or_indexed"])
        self.assertNotIn("action_ids", json.dumps(manifest))
        self.assertEqual(adapter.load_action_labels(self.root),
                         {"alimug_M0001": ["2"], "alimug_M0002": []})
        self.assertEqual(adapter.load_cases(self.root, splits=("test",))[0]["official_split"],
                         "except_TS_test1")

    def test_large_json_cell_exceeds_csv_default_limit(self) -> None:
        record = example_record("M0001", action_ids=[2])
        record["sentences"][0]["s"] = "很长的会议内容" * 30_000
        write_sources(self.root, [record], [example_record("M0002")])
        old_limit = csv.field_size_limit()
        case = adapter.load_cases(self.root, splits=("dev",))[0]
        self.assertGreater(len(case["transcript"]), 128_000)
        self.assertTrue(case["agent_review_transcript_over_limit"])
        self.assertEqual(csv.field_size_limit(), old_limit)

    def test_manifest_rejects_changed_case_or_gold_injection(self) -> None:
        manifest = adapter.build_manifest(self.root)
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(adapter.load_manifest(path, source_dir=self.root), manifest)
        for change in ("transcript", "segment", "gold"):
            altered = copy.deepcopy(manifest)
            if change == "transcript":
                altered["cases"][0]["transcript"] += "X"
            elif change == "segment":
                altered["cases"][0]["segments"][0]["start_ms"] = 0
            else:
                altered["cases"][0]["action_ids"] = [2]
            path.write_text(json.dumps(altered), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(ValueError):
                adapter.load_manifest(path, source_dir=self.root)

    def test_source_checksum_and_source_manifest_lock(self) -> None:
        manifest = adapter.build_manifest(self.root)
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        archive = self.root / "dev.zip"
        original_archive = archive.read_bytes()
        archive.write_bytes(original_archive + b"unexpected")
        with self.assertRaisesRegex(ValueError, "checksum"):
            adapter.load_manifest(path, source_dir=self.root)
        archive.write_bytes(original_archive)
        source_path = self.root / "source-manifest.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["metadata_git_revision"] = "b" * 40
        source_path.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            adapter.load_manifest(path, source_dir=self.root)

    def test_rejects_bad_source_ids_times_and_labels(self) -> None:
        for defect in ("duplicate_id", "missing_action_id", "negative_time", "reverse_time",
                       "out_of_order_time", "empty_text"):
            record = example_record("M0001", action_ids=[2])
            if defect == "duplicate_id":
                record["sentences"][1]["id"] = 1
            elif defect == "missing_action_id":
                record["action_ids"] = [{"id": 3}]
            elif defect == "negative_time":
                record["sentences"][0]["start_time"] = "-1"
            elif defect == "reverse_time":
                record["sentences"][0]["end_time"] = "0.5"
            elif defect == "out_of_order_time":
                record["sentences"][1]["start_time"] = "0.5"
            elif defect == "empty_text":
                record["sentences"][0]["s"] = " "
            write_sources(self.root, [record], [example_record("M0002")])
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                adapter.build_manifest(self.root, splits=("dev",))

    def test_rejects_duplicate_meeting_key_across_splits(self) -> None:
        write_sources(self.root, [example_record("M0001")], [example_record("M0001")])
        with self.assertRaisesRegex(ValueError, "Duplicate meeting key"):
            adapter.build_manifest(self.root)


@unittest.skipUnless((adapter.SOURCE_DIR / "dev.zip").is_file() and
                     (adapter.SOURCE_DIR / "except_TS_test1.zip").is_file(),
                     "Pinned official ZIPs are unavailable")
class OfficialArchiveSmokeTests(unittest.TestCase):
    def test_record_and_label_counts(self) -> None:
        manifest = adapter.build_manifest()
        self.assertEqual(manifest["selection"]["split_counts"], {"dev": 65, "test": 82})
        self.assertEqual(len({case["id"] for case in manifest["cases"]}), 147)
        self.assertTrue(all(case["timestamp_kind"] == "official_seconds"
                            for case in manifest["cases"]))
        labels = adapter.load_action_labels()
        split_by_id = {case["id"]: case["split"] for case in manifest["cases"]}
        self.assertEqual(sum(len(value) for case_id, value in labels.items()
                             if split_by_id[case_id] == "dev"), 222)
        self.assertEqual(sum(len(value) for case_id, value in labels.items()
                             if split_by_id[case_id] == "test"), 236)


if __name__ == "__main__":
    unittest.main()


class VcsumHoldoutTest(unittest.TestCase):
    """2026-09-26：留出集必须排除已消费的 dev22/test26，且抽样确定。"""

    def test_holdout_excludes_consumed_and_is_deterministic(self):
        from benchmarks import vcsum_adapter as adapter
        m1 = adapter.build_holdout_manifest(count=4)
        m2 = adapter.build_holdout_manifest(count=4)
        ids1 = m1["selection"]["selected_meeting_ids"]
        self.assertEqual(ids1, m2["selection"]["selected_meeting_ids"])
        excluded = set(m1["selection"]["consumed_meeting_ids_excluded"])
        self.assertEqual(len(excluded), 48)
        self.assertFalse(set(ids1) & excluded)
        self.assertTrue(all(case["reference_overall"] is None for case in m1["cases"]))
        self.assertEqual(m1["dataset_id"], "vcsum_long_holdout4")
