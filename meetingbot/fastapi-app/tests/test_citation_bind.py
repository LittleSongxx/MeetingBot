import unittest

from services.citation_bind import bind_answer


class CitationBindTest(unittest.TestCase):
    def test_number_in_wrong_citation_is_rebound_before_punctuation(self):
        contexts = [
            {"ref": 1, "text": "讨论了下周计划。"},
            {"ref": 2, "text": "发布会在 9月1日 开始。"},
        ]
        result = bind_answer("发布时间是 9月1日 [1]。", contexts)
        self.assertEqual(result.checked_numbers, 1)
        self.assertEqual(result.rebound, 1)
        self.assertTrue(result.answer.endswith("[1][2]。"), result.answer)

    def test_number_already_supported_is_untouched(self):
        contexts = [{"ref": 1, "text": "发布会 9月1日 开始。"}]
        result = bind_answer("发布时间是 9月1日 [1]。", contexts)
        self.assertEqual(result.already_supported, 1)
        self.assertFalse(result.changed)

    def test_number_absent_everywhere_is_left_alone_and_counted(self):
        contexts = [{"ref": 1, "text": "没有提到那件事。"}]
        result = bind_answer("时间是 2030年1月1日 [1]。", contexts)
        self.assertEqual(result.unbound, 1)
        self.assertEqual(result.unbound_items[0]["token"], "2030年1月1日")
        self.assertFalse(result.changed)

    def test_multiple_numbers_union_refs(self):
        contexts = [
            {"ref": 1, "text": "无关内容。"},
            {"ref": 2, "text": "截止 9月1日。"},
            {"ref": 3, "text": "会议 10:00 开始。"},
        ]
        result = bind_answer("9月1日 10:00 开始 [1]。", contexts)
        self.assertEqual(result.checked_numbers, 2)
        self.assertEqual(result.rebound, 2)
        self.assertIn("[2]", result.answer)
        self.assertIn("[3]", result.answer)

    def test_uncited_sentences_are_not_modified(self):
        contexts = [{"ref": 1, "text": "9月1日 发布。"}]
        result = bind_answer("无引用的一句 9月1日。", contexts)
        self.assertEqual(result.checked_numbers, 0)
        self.assertEqual(result.uncited_numbers, 1)
        self.assertFalse(result.changed)

    def test_citation_not_duplicated(self):
        contexts = [
            {"ref": 1, "text": "无关。"},
            {"ref": 2, "text": "9月1日。"},
        ]
        result = bind_answer("时间 9月1日 [2]。", contexts)
        self.assertEqual(result.already_supported, 1)
        self.assertEqual(result.answer.count("[2]"), 1, result.answer)
        self.assertFalse(result.changed)


if __name__ == "__main__":
    unittest.main()
