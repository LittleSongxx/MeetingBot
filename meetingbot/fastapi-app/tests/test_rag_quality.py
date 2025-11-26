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


if __name__ == "__main__":
    unittest.main()
