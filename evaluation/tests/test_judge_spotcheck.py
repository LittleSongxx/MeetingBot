"""裁判抽检单测：抽样可复现、CSV 标签列留空、Cohen's kappa、无回填路径。

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

from gold import judge_spotcheck  # noqa: E402


def _artifact(count: int = 5) -> dict:
    return {"turns": [
        {
            "case_id": f"q{index:02d}",
            "question": f"问题 {index}",
            "answer": f"答案 {index}",
            "stop_reason": "finished",
            "citations": [{"ref": 1, "kind": "chunk", "quote": f"原文 {index}"}],
            "tools": [{"name": "search_knowledge"}],
        }
        for index in range(1, count + 1)
    ]}


class NormalizeLabelTest(unittest.TestCase):
    def test_synonyms_collapse(self):
        for value in ("grounded", "有依据", "支持", "YES", "1", "true"):
            self.assertEqual(judge_spotcheck.normalize_label(value), "yes")
        for value in ("unsupported", "无依据", "不支持", "NO", "0", "false"):
            self.assertEqual(judge_spotcheck.normalize_label(value), "no")

    def test_empty_and_unknown(self):
        self.assertEqual(judge_spotcheck.normalize_label(None), "")
        self.assertEqual(judge_spotcheck.normalize_label("   "), "")
        self.assertEqual(judge_spotcheck.normalize_label("Maybe"), "maybe")


class CohenKappaTest(unittest.TestCase):
    def test_perfect_agreement_is_one(self):
        result = judge_spotcheck.cohen_kappa(
            ["yes", "yes", "no", "no"], ["yes", "yes", "no", "no"])
        self.assertAlmostEqual(result["observed_agreement"], 1.0)
        self.assertAlmostEqual(result["cohen_kappa"], 1.0)

    def test_chance_level_agreement_is_zero(self):
        result = judge_spotcheck.cohen_kappa(
            ["yes", "yes", "no", "no"], ["yes", "no", "yes", "no"])
        self.assertAlmostEqual(result["observed_agreement"], 0.5)
        self.assertAlmostEqual(result["expected_agreement"], 0.5)
        self.assertAlmostEqual(result["cohen_kappa"], 0.0)

    def test_known_example(self):
        # 20 条：15 条一致（yes/yes），5 条裁判 no、人工 yes
        judge = ["yes"] * 15 + ["no"] * 5
        human = ["yes"] * 20
        result = judge_spotcheck.cohen_kappa(judge, human)
        self.assertAlmostEqual(result["observed_agreement"], 0.75)
        self.assertAlmostEqual(result["expected_agreement"], 0.75)
        self.assertAlmostEqual(result["cohen_kappa"], 0.0)
        self.assertIsNone(judge_spotcheck.cohen_kappa([], []))

    def test_single_category_is_undefined(self):
        result = judge_spotcheck.cohen_kappa(["yes"] * 4, ["yes"] * 4)
        self.assertIsNone(result["cohen_kappa"],
                          "只有单一类别时 kappa 不可定义，不能报 1")

    def test_interpretation_bands(self):
        self.assertIn("almost perfect", judge_spotcheck.kappa_interpretation(0.9))
        self.assertIn("moderate", judge_spotcheck.kappa_interpretation(0.5))
        self.assertIn("不可定义", judge_spotcheck.kappa_interpretation(None))


class RowsTest(unittest.TestCase):
    def test_collect_dedupes_by_question_and_keeps_citations(self):
        rows = judge_spotcheck.collect_rows([
            ("a.json", _artifact(3)),
            ("b.json", _artifact(3)),
        ])
        self.assertEqual(len(rows), 3, "同一 question 多份产物只取第一次出现")
        self.assertEqual(rows[0]["case_id"], "q01")
        self.assertEqual(rows[0]["judge_label"], "")
        self.assertEqual(rows[0]["human_label"], "")
        self.assertIn("原文 1", rows[0]["citations"])

    def test_sample_is_deterministic_and_capped(self):
        rows = judge_spotcheck.collect_rows([("a.json", _artifact(30))])
        first = judge_spotcheck.sample_rows(rows, 20, seed=7)
        second = judge_spotcheck.sample_rows(rows, 20, seed=7)
        self.assertEqual([row["case_id"] for row in first],
                         [row["case_id"] for row in second])
        self.assertEqual(len(first), 20)
        self.assertEqual(len(judge_spotcheck.sample_rows(rows, 100, seed=7)), 30)
        with self.assertRaises(ValueError):
            judge_spotcheck.sample_rows(rows, 0)

    def test_csv_roundtrip_leaves_labels_empty(self):
        rows = judge_spotcheck.sample_rows(
            judge_spotcheck.collect_rows([("a.json", _artifact(3))]), 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spot.csv"
            judge_spotcheck.write_csv(path, rows)
            read_back = judge_spotcheck.read_csv(path)
        self.assertEqual(len(read_back), 3)
        for row in read_back:
            self.assertEqual(row["judge_label"], "")
            self.assertEqual(row["human_label"], "")
            self.assertTrue(row["question"])
            self.assertTrue(row["answer"])


class ScoreRowsTest(unittest.TestCase):
    def _rows(self, pairs):
        return [{"case_id": f"q{index}", "question": f"问题 {index}",
                 "judge_label": judge, "human_label": human}
                for index, (judge, human) in enumerate(pairs, start=1)]

    def test_scores_agreement_and_kappa(self):
        report = judge_spotcheck.score_rows(self._rows(
            [("grounded", "有依据"), ("unsupported", "有依据"),
             ("ungrounded", "无依据")]))
        self.assertEqual(report["n_scored"], 3)
        self.assertEqual(report["n_agree"], 2)
        self.assertAlmostEqual(report["observed_agreement"]["rate"], 2 / 3)
        self.assertAlmostEqual(report["cohen_kappa"], 0.4, places=6)
        # confusion 的键与值都是归一后的标签（yes/no），原始中文在前两列保留
        self.assertEqual(report["confusion"], {"no": {"no": 1},
                                               "yes": {"no": 1, "yes": 1}})
        self.assertEqual(len(report["per_row"]), 3)

    def test_empty_labels_report_no_data(self):
        rows = [{"case_id": "q1", "question": "问题", "judge_label": "", "human_label": ""},
                {"case_id": "q2", "question": "问题2", "judge_label": "yes", "human_label": ""}]
        report = judge_spotcheck.score_rows(rows)
        self.assertEqual(report["n_scored"], 0)
        self.assertEqual(report["n_partially_filled"], 1)
        self.assertIsNone(report["cohen_kappa"])
        self.assertIn("无回填数据", report["reading"])


class CliTest(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = judge_spotcheck.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_generate_then_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_path = root / "answers.json"
            artifact_path.write_text(json.dumps(_artifact(25), ensure_ascii=False),
                                     encoding="utf-8")
            csv_path = root / "spot.csv"
            code, payload = self._run(
                ["--artifact", str(artifact_path), "--sample", "20",
                 "--output", str(csv_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["sample"], 20)
            self.assertEqual(payload["candidates"], 25)
            self.assertEqual(payload["label_columns_left_empty"],
                             ["judge_label", "human_label"])
            rows = judge_spotcheck.read_csv(csv_path)
            self.assertEqual(len(rows), 20)
            for row in rows:
                self.assertEqual(row["judge_label"], "")
                self.assertEqual(row["human_label"], "")
            # 回填：前 15 一致，后 5 不一致
            for index, row in enumerate(rows):
                row["judge_label"] = "grounded" if index < 15 else "unsupported"
                row["human_label"] = "有依据"
            judge_spotcheck.write_csv(csv_path, rows)
            score_json = root / "score.json"
            code, payload = self._run(["--score", str(csv_path),
                                       "--output", str(score_json)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["n_scored"], 20)
            self.assertEqual(payload["n_agree"], 15)
            self.assertAlmostEqual(payload["observed_agreement"]["rate"], 0.75)
            saved = json.loads(score_json.read_text(encoding="utf-8"))
            self.assertEqual(saved["n_scored"], 20)

    def test_no_artifact_reports_no_sample(self):
        code, payload = self._run([])
        self.assertEqual(code, 0)
        self.assertEqual(payload["sample"], 0)
        self.assertIn("无样本", payload["reading"])

    def test_score_empty_csv_reports_no_data(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "spot.csv"
            judge_spotcheck.write_csv(csv_path, judge_spotcheck.collect_rows(
                [("a.json", _artifact(3))]))
            code, payload = self._run(["--score", str(csv_path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["n_scored"], 0)
            self.assertIn("无回填数据", payload["reading"])

    def test_missing_artifact_returns_two(self):
        # 错误路径 stdout 为空，不能复用会解析 JSON 的 _run
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = judge_spotcheck.main(
                ["--artifact", "/tmp/does-not-exist-eval.json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
