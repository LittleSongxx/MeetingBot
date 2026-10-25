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

    def test_recording_fact_citation_carries_duration(self):
        """录音清单引用快照要带工具已返回的时长（否则合法的时长断言判成无依据）。"""
        registry = CitationRegistry()
        registry.annotate("list_facts", {
            "ok": True,
            "kind": "recording",
            "facts": [{
                "task_id": 7, "meeting_id": 2, "meeting_title": "产品发布评审会议",
                "recorded_at": "2026-09-10 14:00:00", "period": "下午",
                "duration_minutes": 4.5,
            }],
        })
        citation = registry.all()[0]
        self.assertEqual(citation.task_id, 7)
        self.assertIn("时长 4.5 分钟", citation.source_text or "")
        self.assertIn("时长 4.5 分钟", citation.quote or "")

    def test_state_citation_carries_tool_notice_and_type(self):
        """状态引用快照要带工具 notice/类型：否则“只返回 CONFIRMED…”这类断言判成无依据。"""
        registry = CitationRegistry()
        registry.annotate("query_meeting_state", {
            "ok": True,
            "kind": "decision",
            "notice": "只返回 CONFIRMED 记忆与有效待办；未确认候选不得当作事实。",
            "items": [{"content": "9月1日正式发布", "kind": "decision",
                       "status": "CONFIRMED", "source_meeting_id": 2,
                       "series_name": "产品发布评审系列"}],
        })
        citation = registry.all()[0]
        self.assertIn("工具说明：只返回 CONFIRMED", citation.source_text or "")
        # 类型/系列是引用卡片上的独立字段，不重复塞进 source_text（避免卡片内容重复）
        self.assertNotIn("类型：DECISION", citation.source_text or "")

    def test_minutes_item_text_carries_evidence_quote(self):
        """纪要条目引用快照要带证据原句（判定者核对引语/字段时用）。"""
        from services.citations import _item_text
        text = _item_text({
            "content": "发布日定在9月1日",
            "owner_suggestion": "王工",
            "evidence": [{"quote": "9月1日上午九点正式发布", "speaker": "陈雪"}],
        })
        self.assertIn("负责人：王工", text)
        self.assertIn("证据：9月1日上午九点正式发布", text)
