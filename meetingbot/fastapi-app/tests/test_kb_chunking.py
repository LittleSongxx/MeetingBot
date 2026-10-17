"""知识库切块的单测：纯函数，不需要数据库与模型。"""

from __future__ import annotations

import unittest

from services import kb_chunking as kc

FIELD_ORDER = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")


def _segments(count: int, *, chars: int = 20, speaker: str = "张涛"):
    """造 count 句，每句 chars 个字，时间戳按 4 秒一句递增。"""
    return [
        (i * 4000, i * 4000 + 3900, speaker if i % 2 == 0 else "王敏", f"第{i}句" + "内容" * (chars // 2))
        for i in range(count)
    ]


class ChunkTranscriptTest(unittest.TestCase):
    def test_empty_input_yields_no_chunks(self):
        self.assertEqual(kc.chunk_transcript([]), [])
        self.assertEqual(kc.chunk_transcript([(0, 100, "甲", "   ")]), [])

    def test_windows_respect_the_target_and_do_not_lose_any_line(self):
        segments = _segments(30)
        chunks = kc.chunk_transcript(segments, target_chars=200, overlap_lines=1)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # 允许单行超目标（那行必须自成窗口），但不得出现"两行时仍远超目标"
            self.assertLessEqual(len(chunk.text), 200 + kc.MAX_SINGLE_LINE_CHARS)
        # 每一句都必须能在某个窗口里找到——切块不能丢内容
        for index in range(30):
            self.assertTrue(any(f"第{index}句" in c.text for c in chunks), f"第{index}句丢了")

    def test_adjacent_windows_overlap(self):
        chunks = kc.chunk_transcript(_segments(30), target_chars=200, overlap_lines=2)
        self.assertGreater(len(chunks), 1)
        first_tail = set(chunks[0].text.splitlines()[-2:])
        second_head = set(chunks[1].text.splitlines()[:2])
        self.assertTrue(first_tail & second_head, "相邻窗口之间必须重叠")

    def test_chunk_numbers_are_sequential_from_zero(self):
        chunks = kc.chunk_transcript(_segments(20), target_chars=200)
        self.assertEqual([c.chunk_no for c in chunks], list(range(len(chunks))))

    def test_line_format_matches_the_minutes_pipeline(self):
        """切片正文行格式必须与 minutes_service.transcript_text 一致。"""
        chunks = kc.chunk_transcript([(65000, 70000, "王敏", "这里有内容")])
        self.assertEqual(chunks[0].text, "[65s][王敏] 这里有内容")

    def test_time_bounds_cover_the_window(self):
        chunks = kc.chunk_transcript(_segments(10), target_chars=120, overlap_lines=0)
        self.assertEqual(chunks[0].start_ms, 0)
        self.assertEqual(chunks[-1].end_ms, 9 * 4000 + 3900)

    def test_overlong_line_without_punctuation_is_split(self):
        chunks = kc.chunk_transcript([(0, 60000, "李悦", "啊" * 3000)])
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), kc.MAX_SINGLE_LINE_CHARS + 64)
        # 内容不能丢：拼回来应等于原文
        joined = "".join(chunk.text.split("] ", 1)[1] for chunk in chunks)
        self.assertEqual(joined, "啊" * 3000)

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            kc.chunk_transcript(_segments(3), target_chars=0)
        with self.assertRaises(ValueError):
            kc.chunk_transcript(_segments(3), overlap_lines=-1)

    def test_overlap_larger_than_window_terminates(self):
        """重叠句数大于窗口长度时不能死循环。"""
        chunks = kc.chunk_transcript(_segments(6), target_chars=60, overlap_lines=99)
        self.assertGreater(len(chunks), 1)


class ChunkMinutesTest(unittest.TestCase):
    def setUp(self):
        self.summary = {"text": "本次会议审定 V2.0 发布范围", "basis": "stated"}
        self.items = {
            "decisions": [{
                "content": "发布时先隐藏报表导出入口",
                "owner": {"text": "张涛"},
                "deadline": {"text": "本周五", "normalized": "2026-09-18"},
                "origin": "stated",
                "evidence": [{"quote": "发布时先隐藏入口", "speaker": "张涛"}],
            }],
            "risks": [{"content": "异步改造与灰度挨得太近", "level": "HIGH", "suggestion": "周三给中期进度"}],
        }

    def test_fields_follow_the_supplied_order(self):
        chunks = kc.chunk_minutes(self.summary, self.items, field_order=FIELD_ORDER)
        self.assertEqual([c.field for c in chunks], ["summary", "decisions", "risks"])

    def test_item_keeps_index_origin_and_evidence(self):
        chunks = kc.chunk_minutes(self.summary, self.items, field_order=FIELD_ORDER)
        decision = next(c for c in chunks if c.field == "decisions")
        self.assertEqual(decision.item_index, 0)
        self.assertEqual(decision.origin, "stated")
        self.assertEqual(len(decision.evidence), 1)
        self.assertEqual(decision.chunk_type, kc.CHUNK_MINUTES_ITEM)

    def test_typed_slots_include_the_normalized_value(self):
        """归一化值要写进切片正文，否则"9月18日"搜不到只写了"本周五"的那条。"""
        chunks = kc.chunk_minutes(self.summary, self.items, field_order=FIELD_ORDER)
        decision = next(c for c in chunks if c.field == "decisions")
        self.assertIn("本周五（2026-09-18）", decision.text)
        self.assertIn("张涛", decision.text)

    def test_risk_level_is_searchable(self):
        chunks = kc.chunk_minutes(self.summary, self.items, field_order=FIELD_ORDER)
        risk = next(c for c in chunks if c.field == "risks")
        self.assertIn("风险等级 HIGH", risk.text)

    def test_summary_has_no_item_index(self):
        chunks = kc.chunk_minutes(self.summary, self.items, field_order=FIELD_ORDER)
        self.assertIsNone(chunks[0].item_index)

    def test_missing_and_empty_fields_are_skipped(self):
        chunks = kc.chunk_minutes(None, {"decisions": []}, field_order=FIELD_ORDER)
        self.assertEqual(chunks, [])

    def test_blank_items_are_skipped(self):
        chunks = kc.chunk_minutes(None, {"decisions": [{"content": "   "}]}, field_order=FIELD_ORDER)
        self.assertEqual(chunks, [])


if __name__ == "__main__":
    unittest.main()


class ChunkSizeConfigTest(unittest.TestCase):
    """窗口大小是**可配参数**：会议转写该用多大，要在本文料上用检索指标定。"""

    def test_env_overrides_the_default(self):
        from unittest import mock

        from services import kb_chunking
        with mock.patch.dict("os.environ", {"KB_CHUNK_TARGET_CHARS": "300"}):
            self.assertEqual(kb_chunking.target_chars(), 300)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("KB_CHUNK_TARGET_CHARS", None)
            self.assertEqual(kb_chunking.target_chars(), kb_chunking.DEFAULT_TARGET_CHARS)

    def test_invalid_values_fail_loudly(self):
        from unittest import mock

        from services import kb_chunking
        for value in ("abc", "50", "0"):
            with mock.patch.dict("os.environ", {"KB_CHUNK_TARGET_CHARS": value}), \
                    self.assertRaises(ValueError):
                kb_chunking.target_chars()
