"""新仪器的离线单测：轴三（人工协作成本）、pass^k、AID 锚点、库导出。

**全部零调用、不访问网络、不连数据库。** 数据库相关的部分只测纯函数与产物形状。
"""

from __future__ import annotations

import json
import unittest

from gold import annotation
from judges import completeness
from lib import model_client


if __name__ == "__main__":
    unittest.main()


class AnnotationTest(unittest.TestCase):
    """标注管线：α、逐类别、PABAK；协议是 verification。"""

    def test_alpha_is_one_on_perfect_agreement(self):
        result = annotation.krippendorff_alpha([["a", "a", "a"], ["b", "b"], ["c", "c", "c"]])
        self.assertAlmostEqual(result["alpha"], 1.0)

    def test_alpha_matches_hand_computed_values(self):
        """用**手算**核对，而不是拿实现去印证实现。

        单位 (a,b)/(b,a)/(a,b)/(b,a)：n_ab = n_ba = 4、n = 8、Do = 8/8 = 1；
        边际 n_a = n_b = 4，De = (4·4 + 4·4)/(8·7) = 0.5714 ⇒ α = 1 − 1/0.5714 = −0.75。
        交替对立的一致度**低于随机**，α 为负是正确行为，不是 bug。
        """
        result = annotation.krippendorff_alpha([["a", "b"], ["b", "a"], ["a", "b"], ["b", "a"]])
        self.assertAlmostEqual(result["alpha"], -0.75, places=6)

    def test_two_units_with_opposite_codings_is_below_chance(self):
        """手算（两个单位，别只算一个）：n_ab = n_ba = 2、对角 0、n = 4；
        Do = 4/4 = 1；边际各 2，De = (2·2+2·2)/(4·3) = 0.6667 ⇒ α = −0.5。"""
        result = annotation.krippendorff_alpha([["a", "b"], ["b", "a"]])
        self.assertAlmostEqual(result["alpha"], -0.5, places=6)

    def test_partial_agreement_lands_above_chance(self):
        """手算：(a,a),(a,b),(b,a),(b,b) ⇒ 对角 a,b 各 2·1/1 = 2，n_ab = n_ba = 2，n = 8；
        Do = 4/8 = 0.5；边际各 4，De = (4·4+4·4)/(8·7) = 0.5714 ⇒ α = 0.125。"""
        result = annotation.krippendorff_alpha([["a", "a"], ["a", "b"], ["b", "a"], ["b", "b"]])
        self.assertAlmostEqual(result["alpha"], 0.125, places=6)

    def test_units_with_fewer_than_two_coders_are_skipped(self):
        result = annotation.krippendorff_alpha([["a"], ["b", "b"], [None, None]])
        self.assertEqual(result["units"], 1)

    def test_per_category_alpha_is_reported_published_threshold_stated(self):
        """整体 α 0.7 可能掩盖某一类 0.2，因此逐类别必须一起报。"""
        result = annotation.krippendorff_alpha([["correct", "correct"], ["na", "na"],
                                                ["missing", "wrong"]])
        self.assertIn("per_category", result)
        self.assertIn("correct", result["per_category"])
        self.assertIn("α ≥ 0.7", result["published_threshold"])

    def test_pabak_complements_alpha_under_skewed_prevalence(self):
        """偏斜患病率会把 α 压扁（kappa paradox），所以 PABAK 要一起报。"""
        units = [["yes", "yes"]] * 9 + [["no", "yes"]]
        result = annotation.pabak(units)
        self.assertAlmostEqual(result["agreement"], 9 / 10)
        self.assertAlmostEqual(result["pabak"], 0.8)

    def test_verification_protocol_labels_keep_missing_and_na_apart(self):
        """少了 missing 与 na，"没提到"和"该有但漏了"会被挤进一个桶，两类都无法解释。"""
        self.assertIn("missing", annotation.OWNER_DEADLINE_LABELS)
        self.assertIn("na", annotation.OWNER_DEADLINE_LABELS)
        self.assertIn("verification", annotation.build_owner_deadline_packet.__doc__)

    def test_packet_requires_at_least_three_annotators_and_raw_publishing(self):
        packet = annotation.build_owner_deadline_packet(sample=1, use_db=False)
        self.assertIn("≥3", packet["annotators_required"])
        self.assertIn("原始标注", packet["publish_raw"])


class ProviderRegistryTest(unittest.TestCase):
    """供应商注册表清理后的锁定（2026-09-26：glm/kimi 已按决定移除）。"""

    def test_glm_and_kimi_are_removed(self):
        self.assertNotIn("glm", model_client.PROVIDERS)
        self.assertNotIn("kimi", model_client.PROVIDERS)

    def test_version_segment_urls_build_correctly(self):
        """URL 拼接按「版本段」类别处理：/v1、/v4 都直接拼 chat/completions。

        该逻辑原由 glm（智谱 /api/paas/v4 基地址）引入，注册项移除后用注入的
        假 spec 锁住，防止未来接入新族时回归成 /v4/v1/chat/completions。"""
        from unittest import mock
        import os
        fake = {"base_env": "FAKE_BASE", "model_env": "FAKE_MODEL",
                "key_env": "FAKE_KEY", "fallback_key_env": "",
                "base_default": "https://example.com/api/paas/v4",
                "model_default": "fake-model"}
        model_client.PROVIDERS["_fake"] = fake
        try:
            with mock.patch.dict(os.environ, {
                    "FAKE_BASE": "https://example.com/api/paas/v4",
                    "FAKE_MODEL": "fake-model", "FAKE_KEY": "k"}, clear=False):
                url, _model, _key = model_client.endpoint("_fake")
            self.assertEqual(url, "https://example.com/api/paas/v4/chat/completions")
        finally:
            del model_client.PROVIDERS["_fake"]


class CompletenessScaffoldArtifactTest(unittest.TestCase):
    """2026-09-26 审计发现：无条件截断声明会被判定者当证据用（meeting_level 已修，
    completeness 当时漏修）。这些测试锁住条件化行为。"""

    def test_no_truncation_claim_when_not_truncated(self):
        header = completeness._transcript_header("短转写" * 10, 60000)
        self.assertNotIn("截断", header)

    def test_truncation_claim_only_when_actually_truncated(self):
        header = completeness._transcript_header("字" * 70000, 60000)
        self.assertIn("已截断到前 60000 字符", header)
        self.assertIn("不能据此断言后文不存在相关内容", header)

    def test_generate_prompt_has_no_unconditional_disclaimer(self):
        # 源码级回归锁：generate_keypoints / judge_coverage 的提示词里
        # 不允许再出现「可能被截断」这一无条件措辞。
        import inspect
        for func in (completeness.generate_keypoints, completeness.judge_coverage):
            source = inspect.getsource(func)
            self.assertNotIn("可能被截断", source)


class CerToolTest(unittest.TestCase):
    """⑥ CER 工具：三口径与标点/全半角/数字归一（2026-09-26）。"""

    def test_perfect_match_is_zero_all_variants(self):
        from asr import cer_tool
        self.assertEqual(cer_tool.cer("今天开会讨论甲事项", "今天开会讨论甲事项"),
                         {"raw": 0.0, "no_punct": 0.0, "normalized": 0.0})

    def test_punctuation_counts_only_in_raw(self):
        from asr import cer_tool
        result = cer_tool.cer("今天，开会。", "今天开会")
        self.assertGreater(result["raw"], 0)
        self.assertEqual(result["no_punct"], 0.0)

    def test_fullwidth_and_chinese_numerals_normalized(self):
        from asr import cer_tool
        result = cer_tool.cer("增长了５０％", "增长了50%")
        self.assertEqual(result["normalized"], 0.0)
        self.assertGreater(result["raw"], 0)
        result2 = cer_tool.cer("第三个议题", "第3个议题")
        self.assertEqual(result2["normalized"], 0.0)
        # 复合汉字数（三百 vs 300）不做归一——逐字替换必错，属对齐层职责
        result3 = cer_tool.cer("大约三百人参加", "大约300人参加")
        self.assertGreater(result3["normalized"], 0)

    def test_substitution_deletion_insertion(self):
        from asr import cer_tool
        # 替换 1 + 插入 1 / 参考 4 字 → (1+1)/4 = 0.5
        self.assertEqual(cer_tool.cer("甲乙丙丁", "甲X丙丁Y")["raw"], 0.5)

    def test_empty_reference_returns_none_not_zero(self):
        from asr import cer_tool
        self.assertIsNone(cer_tool.cer("", "假设")["raw"])
