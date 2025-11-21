"""引用登记的单测：片段引用必须带来源身份（source_id）。

实测背景（2026-10-17 扩展集 q90）：`get_transcript_excerpt` 返回的片段引用只带
`task_id`，评测侧按 `source_id` 映射回录音时判不了，导致 112 条片段被误记成
"没有时间轴"。两者同值，但字段语义不同：`task_id` 是工具参数，`source_id` 是
引用身份；缺任何一个都会让另一侧静默退化。
"""

from __future__ import annotations

import unittest

from services.citations import CitationRegistry


class SegmentCitationTest(unittest.TestCase):
    def test_segments_carry_source_identity(self):
        registry = CitationRegistry()
        annotated = registry.annotate("get_transcript_excerpt", {
            "ok": True,
            "meeting": {"id": 2, "title": "产品发布评审会议"},
            "task_id": 7,
            "segments": [{
                "segment_no": 49, "start_second": 233, "end_second": 240,
                "speaker": "系统管理员", "text": "结论复述一遍",
            }],
        })
        self.assertEqual(annotated["segments"][0]["ref"], 1)
        citation = registry.all()[0]
        self.assertEqual(citation.task_id, 7)
        self.assertEqual(citation.source_id, 7)
        self.assertEqual(citation.as_dict()["source_id"], 7)
        self.assertEqual(citation.as_dict()["task_id"], 7)
