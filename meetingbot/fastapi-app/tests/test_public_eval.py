import json
import tempfile
import unittest
from pathlib import Path

from evaluation.adapters.mug import load_mug
from evaluation.adapters.qmsum import load_qmsum
from evaluation.adapters.vcsum import load_vcsum
from evaluation.public_eval import evaluate_mug, evaluate_qmsum, evaluate_vcsum
from evaluation.public_metrics import char_rouge, lexical_overlap_score, span_prf, tokens


def _gold_labels(text: str, start: int = 0, end: int | None = None) -> list[int]:
    end = len(text) if end is None else end
    return [1 if start <= index < end else 0 for index in range(len(text))]


class PublicMetricsTest(unittest.TestCase):
    def test_span_prf(self):
        result = span_prf({1, 2, 3}, {2, 3, 4})
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (2, 1, 1))
        self.assertAlmostEqual(result["f1"], 2 / 3, places=4)

    def test_char_rouge(self):
        result = char_rouge("会议决定改为异步导出", "会议决定改为异步导出")
        self.assertEqual(result, {"rouge1": 1.0, "rouge2": 1.0, "rougeL": 1.0})
        self.assertEqual(char_rouge("", "abc"), {"rouge1": 0.0, "rouge2": 0.0, "rougeL": 0.0})

    def test_tokens_keep_english_words_separate(self):
        self.assertIn("grad", tokens("What did Grad B say"))
        self.assertIn("导出", tokens("决定改为异步导出"))

    def test_lexical_overlap(self):
        self.assertGreater(lexical_overlap_score("What did Grad B say", "Grad B said the structure"), 0.2)


class PublicAdapterTest(unittest.TestCase):
    def test_mug_adapter(self):
        raw = {
            "meeting_key": "M1",
            "sentences": [
                {"id": 1, "speaker": "no.0", "start_time": "1.0", "end_time": "3.0", "s": "张涛负责评估方案。"},
                {"id": 2, "speaker": "no.1", "start_time": "3.0", "end_time": "5.0", "s": "前端联调。"},
            ],
            "action_ids": [{"id": 1}],
            "candidate": [{"key_sentence": ["1"]}],
            "topic_segment_ids": [{"id": 10, "candidate": [{"title": "方案", "key_sentence": ["1", "2"]}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mug.jsonl"
            path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
            cases = load_mug(path)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(len(case["transcript"]), 2)
        self.assertEqual(case["transcript"][0]["speaker"], "no.0")
        self.assertEqual(case["transcript"][0]["start_ms"], 1000)
        self.assertEqual(case["gold"]["action_segments"], [1])
        self.assertEqual(case["gold"]["key_sentences"], [1])
        self.assertEqual(case["gold"]["topics"][0]["titles"], ["方案"])

        keyword = evaluate_mug(cases, "keyword")
        self.assertGreater(keyword["metrics"]["action_f1"], 0)
        oracle = evaluate_mug(cases, "oracle")
        self.assertEqual(oracle["metrics"]["action_f1"], 1.0)

    def test_mug_csv_adapter(self):
        raw = {
            "meeting_key": "M2",
            "sentences": [{"id": 1, "speaker": "no.0", "start_time": "0", "end_time": "1", "s": "张涛负责上线。"}],
            "action_ids": [{"id": 1}],
            "candidate": [{"key_sentence": ["1"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mug.csv"
            path.write_text("idx\tcontent\n0\t" + json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
            cases = load_mug(path)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["gold"]["action_segments"], [1])

    def test_qmsum_adapter(self):
        raw = {
            "meeting_transcripts": [
                {"speaker": "A", "content": "The structure uses a layer."},
                {"speaker": "B", "content": "We need more nodes."},
                {"speaker": "A", "content": "Next step is refinement."},
            ],
            "specific_query_list": [
                {"query": "What did Grad B say about structure?", "answer": "More nodes.", "relevant_text_span": [["1", "2"]]}
            ],
            "general_query_list": [{"query": "Summarize the meeting", "answer": "They discussed structure."}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qmsum.json"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            cases = load_qmsum(path)
        case = cases[0]
        self.assertEqual(len(case["transcript"]), 3)
        self.assertEqual(case["gold"]["queries"][1]["evidence_segments"], [1, 2])
        oracle = evaluate_qmsum(cases, "oracle")
        self.assertEqual(oracle["metrics"]["evidence_f1"], 1.0)
        # lexical baseline 至少要能跑出数值，不要求质量
        lexical = evaluate_qmsum(cases, "lexical")
        self.assertIn("evidence_f1", lexical["metrics"])

    def test_vcsum_adapter(self):
        first = "第一句话。"
        second = "第二句话。"
        third = "第三句话。"
        context = {
            "id": "1",
            "av_num": 1,
            "eos_index": [1],
            "context": [[first, second], [third]],
            "speaker": [1, 2],
        }
        highlights = {
            "id": "1",
            "av_num": 1,
            "eos_index": [1],
            "highlights": [[_gold_labels(first, 0, 0), _gold_labels(second, 0, 0)], [_gold_labels(third)]],
        }
        long_row = {"id": "1", "av_num": 1, "eos_index": [1], "summary": "第三句话是重点。"}
        short_row = {
            "id": "1_0",
            "av_num": 1,
            "context": [[first]],
            "speaker": [1],
            "discussion": "第一句话。",
            "agenda": "标题",
            "highlights": [[_gold_labels(first)]],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "overall_context.txt").write_text(json.dumps(context, ensure_ascii=False) + "\n", encoding="utf-8")
            (root / "overall_highlights.txt").write_text(json.dumps(highlights, ensure_ascii=False) + "\n", encoding="utf-8")
            (root / "long_test.txt").write_text(json.dumps(long_row, ensure_ascii=False) + "\n", encoding="utf-8")
            (root / "short_test.txt").write_text(json.dumps(short_row, ensure_ascii=False) + "\n", encoding="utf-8")
            long_cases = load_vcsum(root, split="test", kind="long")
            short_cases = load_vcsum(root, split="test", kind="short")
        self.assertEqual(len(long_cases), 1)
        self.assertEqual(len(long_cases[0]["transcript"]), 3)
        self.assertEqual(long_cases[0]["gold"]["evidence_segments"], [2])
        self.assertEqual(long_cases[0]["gold"]["summary"], "第三句话是重点。")
        self.assertEqual(short_cases[0]["gold"]["title"], "标题")
        lead = evaluate_vcsum(long_cases, "lead")
        self.assertIn("rouge1", lead["metrics"])
        oracle = evaluate_vcsum(long_cases, "oracle")
        self.assertEqual(oracle["metrics"]["rouge1"], 1.0)
        self.assertEqual(oracle["metrics"]["evidence_f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
