import unittest
from unittest.mock import patch

from services import citations, tool_metadata


class ToolEnrichmentTest(unittest.TestCase):
    def test_flag_defaults_off_and_reads_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(tool_metadata.enrichment_enabled())
        with patch.dict("os.environ", {"CHAT_TOOL_ENRICHMENT_ENABLED": "1"}):
            self.assertTrue(tool_metadata.enrichment_enabled())

    def test_recording_fact_citation_includes_series_metadata(self):
        result = {"kind": "recording", "facts": [{
            "task_id": 6, "meeting_id": 2, "meeting_title": "发布评审",
            "recorded_at": "2026-09-10 09:00:00", "period": "上午",
            "duration_minutes": 15.9,
            "series_id": 1, "series_name": "产品发布评审系列",
            "series_meeting_count": 1, "series_meeting_titles": ["2·发布评审"],
        }]}
        pairs = citations._from_facts(result)
        text = pairs[0][1].source_text or ""
        self.assertIn("系列：产品发布评审系列", text)
        self.assertIn("系列内会议 1 场", text)

    def test_state_citation_includes_confirmation_and_supersedes(self):
        result = {"kind": "decision", "notice": "只返回 CONFIRMED", "items": [{
            "content": "9月1日发布", "status": "CONFIRMED", "state_type": "DECISION",
            "state_id": 1, "minutes_status": "DRAFT", "supersedes_id": None,
            "series_name": "产品发布评审系列", "series_meeting_count": 1,
            "series_meeting_titles": ["2·发布评审"],
        }]}
        pairs = citations._from_state(result)
        text = pairs[0][1].source_text or ""
        self.assertIn("来源纪要状态：DRAFT", text)
        self.assertIn("替代关系：无", text)
        self.assertIn("系列内会议 1 场", text)

    def test_state_citation_unchanged_without_enrichment_fields(self):
        result = {"kind": "decision", "notice": "", "items": [{
            "content": "旧条目", "state_type": "DECISION",
        }]}
        pairs = citations._from_state(result)
        text = pairs[0][1].source_text or ""
        self.assertNotIn("来源纪要状态", text)
        self.assertNotIn("替代关系", text)


    def test_state_citation_supersedes_uses_production_kind_field(self):
        """生产 query_meeting_state 条目用 kind，不是 state_type；必须同样进 supersedes 分支。"""
        result = {"kind": "decision", "notice": "", "items": [{
            "content": "9月1日发布", "kind": "decision", "status": "CONFIRMED",
            "state_id": 1, "supersedes_id": None,
        }]}
        pairs = citations._from_state(result)
        text = pairs[0][1].source_text or ""
        self.assertIn("替代关系：无", text)

    def test_state_citation_includes_user_scope(self):
        result = {"kind": "preference", "notice": "", "items": [{
            "content": "回答先给结论", "kind": "preference", "status": "CONFIRMED",
            "scope_type": "USER", "scope_id": 1,
        }]}
        pairs = citations._from_state(result)
        self.assertIn("用户 1", pairs[0][1].source_text or "")

if __name__ == "__main__":
    unittest.main()
