"""RAG 质量闸确定性审计单测：零模型。"""
from __future__ import annotations

import unittest

from services import rag_quality


class RagQualityTest(unittest.TestCase):
    def test_supported_number_passes(self):
        flags = rag_quality.audit_answer(
            "正式发布时间是9月1日9:00[1]。",
            [{"ref": 1, "quote": "同意9月1日9:00正式发布"}])
        self.assertEqual(flags, [])

    def test_number_not_in_cited_is_flagged(self):
        flags = rag_quality.audit_answer(
            "正式发布时间是9月1日9:00[1]。",
            [{"ref": 1, "quote": "发布时间待定", "source_text": "发布范围"}])
        self.assertTrue(any(flag["kind"] == "number_not_in_cited" for flag in flags))

    def test_uncited_number_is_flagged(self):
        flags = rag_quality.audit_answer("正式发布时间是9月1日9:00。", [])
        self.assertTrue(any(flag["kind"] == "uncited_number" for flag in flags))

    def test_quote_not_in_cited_is_flagged(self):
        flags = rag_quality.audit_answer(
            "李月说：“同意9月1日发布”[1]。",
            [{"ref": 1, "quote": "其他内容", "source_text": "发布范围"}])
        self.assertTrue(any(flag["kind"] == "quote_not_in_cited" for flag in flags))

    def test_missing_ref_is_flagged(self):
        flags = rag_quality.audit_answer("发布时间是9月1日9:00[2]。",
                                          [{"ref": 1, "quote": "9月1日9:00"}])
        self.assertTrue(any(flag["kind"] == "missing_citation" for flag in flags))

    def test_number_attribution_mismatch_is_distinguished(self):
        """证据在本轮其它上下文里 → 归属错（可修复），不是"编造"。"""
        flags = rag_quality.audit_answer(
            "正式发布时间是9月1日9:00[1]。",
            [{"ref": 1, "quote": "发布时间待定"}],
            retrieved=[{"ref": 1, "quote": "发布时间待定"},
                       {"ref": 2, "quote": "定于9月1日9:00正式发布"}])
        kinds = [flag["kind"] for flag in flags]
        self.assertIn("number_attribution_mismatch", kinds)
        self.assertNotIn("number_not_in_cited", kinds)
        mismatch = next(flag for flag in flags
                        if flag["kind"] == "number_attribution_mismatch")
        self.assertEqual(mismatch["suggested_refs"], [2])

    def test_quote_attribution_mismatch_is_distinguished(self):
        flags = rag_quality.audit_answer(
            "李月说：“同意9月1日发布”[1]。",
            [{"ref": 1, "quote": "其他内容"}],
            retrieved=[{"ref": 1, "quote": "其他内容"},
                       {"ref": 3, "quote": "李月：同意9月1日发布"}])
        kinds = [flag["kind"] for flag in flags]
        self.assertIn("quote_attribution_mismatch", kinds)
        self.assertNotIn("quote_not_in_cited", kinds)

    def test_without_retrieved_keeps_legacy_kind(self):
        flags = rag_quality.audit_answer(
            "正式发布时间是9月1日9:00[1]。",
            [{"ref": 1, "quote": "发布时间待定"}])
        self.assertTrue(any(flag["kind"] == "number_not_in_cited" for flag in flags))


if __name__ == "__main__":
    unittest.main()
