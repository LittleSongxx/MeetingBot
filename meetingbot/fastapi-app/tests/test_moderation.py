"""内容审核的单测：DFA 匹配、三层各自的职责、三种处置动作、留痕不含上下文。

核心不变量只有一条：**命中必须可追溯**（留痕与日志都有），而"命中之后做什么"
由配置决定（flag/redact/block）——三种动作都要有测试，否则切换配置时才发现某条路径没实现。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from services import moderation


class _Words:
    """临时给审核器一份词表（词表缓存按内容签名失效，测试之间不会串）。"""

    def __init__(self, *words: str):
        self.words = ",".join(words)

    def __enter__(self):
        self._patch = mock.patch.dict(os.environ, {"MODERATION_WORDS": self.words})
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class DfaTest(unittest.TestCase):
    def test_matches_words_and_reports_positions(self):
        with _Words("机密", "内部资料"):
            hits = moderation.scan("这是机密内容，不是内部资料。")
        self.assertEqual([hit.word for hit in hits], ["机密", "内部资料"])
        self.assertEqual((hits[0].start, hits[0].end), (2, 4))

    def test_overlapping_words_are_both_reported(self):
        with _Words("上线", "上线时间"):
            hits = moderation.scan("上线时间未定")
        self.assertEqual(sorted(hit.word for hit in hits), ["上线", "上线时间"])

    def test_no_wordlist_means_disabled_not_silently_clean(self):
        with mock.patch.dict(os.environ, {"MODERATION_WORDS": "", "MODERATION_WORDS_FILE": ""}):
            verdict = moderation.check_input("任意内容")
            status = moderation.status()
        self.assertFalse(verdict.enabled)
        self.assertFalse(verdict.matched)
        self.assertFalse(status["enabled"])
        # 未启用时必须如实说明，否则上游会以为"扫过了，没命中"
        self.assertIn("未配置审核词表", status["note"])

    def test_wordlist_file_is_supported_and_missing_file_is_visible(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "words.txt"
            path.write_text("# 注释行\n机密\n\n内部\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MODERATION_WORDS": "",
                                              "MODERATION_WORDS_FILE": str(path)}):
                hits = moderation.scan("机密文件")
            self.assertEqual([hit.word for hit in hits], ["机密"])
        with mock.patch.dict(os.environ, {"MODERATION_WORDS": "",
                                          "MODERATION_WORDS_FILE": "/nope/words.txt"}), \
                self.assertLogs("meetingbot.moderation", level="ERROR"):
            self.assertFalse(moderation.enabled())

    def test_empty_text_is_safe(self):
        with _Words("机密"):
            self.assertEqual(moderation.scan(""), [])


class LayerTest(unittest.TestCase):
    """三层各自的动作：输入拦、约束拼进提示词、输出复检。"""

    def test_input_hit_asks_for_blocking(self):
        with _Words("机密"):
            verdict = moderation.check_input("请把机密讲一遍")
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.action, moderation.ACTION_BLOCK)

    def test_prompt_constraint_only_appears_when_enabled(self):
        with mock.patch.dict(os.environ, {"MODERATION_WORDS": "", "MODERATION_WORDS_FILE": ""}):
            self.assertEqual(moderation.prompt_constraints(), "")
        with _Words("机密"):
            self.assertIn("不得包含违法违规", moderation.prompt_constraints())

    def test_output_action_defaults_to_flag(self):
        with _Words("机密"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODERATION_OUTPUT_ACTION", None)
            verdict = moderation.check_output("这段里有机密")
        self.assertEqual(verdict.action, moderation.ACTION_FLAG)
        text, blocked = moderation.apply_output("这段里有机密", verdict)
        self.assertEqual(text, "这段里有机密")
        self.assertFalse(blocked)

    def test_redact_action_replaces_matched_spans(self):
        # 词表与处置动作必须同时生效：词表上下文退出后再扫就什么都命中不了
        with _Words("机密"), mock.patch.dict(os.environ, {"MODERATION_OUTPUT_ACTION": "redact"}):
            verdict = moderation.check_output("前面机密后面")
            text, blocked = moderation.apply_output("前面机密后面", verdict)
        self.assertEqual(text, "前面***后面")
        self.assertFalse(blocked)

    def test_block_action_returns_no_text(self):
        with _Words("机密"), mock.patch.dict(os.environ, {"MODERATION_OUTPUT_ACTION": "block"}):
            verdict = moderation.check_output("全是机密")
            text, blocked = moderation.apply_output("全是机密", verdict)
        self.assertEqual(text, "")
        self.assertTrue(blocked)

    def test_invalid_action_is_rejected_loudly(self):
        with mock.patch.dict(os.environ, {"MODERATION_OUTPUT_ACTION": "whatever"}), \
                self.assertRaises(ValueError):
            moderation.output_action()

    def test_multiple_hits_are_redacted_without_position_drift(self):
        with _Words("甲", "乙"):
            verdict = moderation.check_output("甲和乙")
            text = moderation.redact("甲和乙", verdict.hits)
        self.assertEqual(text, "***和***")


class TraceTest(unittest.TestCase):
    def test_trace_records_words_and_positions_but_not_context(self):
        with _Words("机密"):
            verdict = moderation.check_output("这是机密内容")
        trace = verdict.as_dict()
        self.assertTrue(trace["matched"])
        self.assertEqual(trace["words"], ["机密"])
        self.assertEqual(trace["positions"], [[2, 4]])
        # 留痕不含上下文：违规原文不该被复制到日志/数据库里再存一份
        self.assertNotIn("这是", str(trace))

    def test_hits_are_logged(self):
        with _Words("机密"), \
                self.assertLogs("meetingbot.moderation", level="WARNING") as logs:
            moderation.check_output("这是机密")
        self.assertIn("命中", logs.output[0])


if __name__ == "__main__":
    unittest.main()
