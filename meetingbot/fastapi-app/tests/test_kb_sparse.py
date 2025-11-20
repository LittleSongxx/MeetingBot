"""知识库词法臂（稀疏编码）的单测：纯函数，零依赖。"""

from __future__ import annotations

import math
import unittest

from services import kb_sparse


class TokenizeTest(unittest.TestCase):
    def test_ascii_words_are_lowercased_and_kept(self):
        self.assertIn("v2.0", kb_sparse.tokenize("V2.0 按期发布"))
        self.assertIn("mtg2026", kb_sparse.tokenize("会议号 MTG2026"))

    def test_single_ascii_letter_is_dropped(self):
        """单个字母多来自 ASR 误听，进索引只会制造噪声。"""
        self.assertNotIn("a", kb_sparse.tokenize("a 项目"))

    def test_chinese_becomes_bigrams(self):
        tokens = kb_sparse.tokenize("报表导出")
        self.assertEqual(tokens, ["报表", "表导", "导出"])

    def test_single_chinese_char_is_kept_as_unigram(self):
        self.assertEqual(kb_sparse.tokenize("好"), ["好"])

    def test_bigrams_do_not_cross_punctuation(self):
        """跨标点拼出的 bigram 没有意义，且会稀释真正有用的词。"""
        tokens = kb_sparse.tokenize("导出。问题")
        self.assertNotIn("出问", tokens)
        self.assertIn("导出", tokens)
        self.assertIn("问题", tokens)

    def test_empty_text(self):
        self.assertEqual(kb_sparse.tokenize(""), [])


class EncodeTest(unittest.TestCase):
    def test_empty_text_encodes_to_empty_vector(self):
        self.assertEqual(kb_sparse.encode(""), {"indices": [], "values": []})
        self.assertEqual(kb_sparse.encode("   "), {"indices": [], "values": []})

    def test_indices_are_sorted_and_unique(self):
        vector = kb_sparse.encode("报表导出报表导出报表")
        self.assertEqual(vector["indices"], sorted(vector["indices"]))
        self.assertEqual(len(vector["indices"]), len(set(vector["indices"])))

    def test_values_are_l2_normalized(self):
        vector = kb_sparse.encode("报表导出接口性能问题")
        norm = math.sqrt(sum(v * v for v in vector["values"]))
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_repeated_terms_get_higher_weight_than_single_occurrence(self):
        vector = kb_sparse.encode("报表 报表 报表 导出")
        alone = kb_sparse.encode("报表 导出")
        # 同一 token 在两份文本里的下标一致，直接比权重
        repeated_index = kb_sparse.encode("报表")["indices"][0]
        self.assertGreater(dict(zip(vector["indices"], vector["values"], strict=True))[repeated_index],
                           dict(zip(alone["indices"], alone["values"], strict=True))[repeated_index])

    def test_encoding_is_stable_across_processes(self):
        """哈希必须可复现——内置 hash() 每进程加盐，会导致写进去的索引查不出来。"""
        first = kb_sparse.encode("产品发布评审会议")
        second = kb_sparse.encode("产品发布评审会议")
        self.assertEqual(first, second)
        # 固定期望值：换哈希函数会让向量库里已有的索引全部失效，必须显式改并重建
        self.assertEqual(kb_sparse.encode("报表")["indices"], [802642])

    def test_index_space_stays_within_uint32(self):
        vector = kb_sparse.encode("任意文本" * 50)
        self.assertTrue(all(0 <= i < (1 << 20) for i in vector["indices"]))


if __name__ == "__main__":
    unittest.main()
