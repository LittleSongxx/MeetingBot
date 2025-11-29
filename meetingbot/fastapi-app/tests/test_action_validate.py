import unittest

from services.action_validate import (
    deadline_status,
    evidence_status,
    owner_status,
    validate_item,
)

TRANSCRIPT = (
    "[第1轮][发言人1] 我们争取在今年的8月份能够全面的上线。\n"
    "[第2轮][发言人2] 徐汇区一网统管3.0版争取在今年8月份全面上线。\n"
    "[第3轮][发言人3] 罗鹏主任提出要重视密评工作。"
)


class ActionValidateTest(unittest.TestCase):
    def test_owner_located_and_fabricated(self):
        self.assertEqual(owner_status("徐汇区", TRANSCRIPT), "located")
        self.assertEqual(owner_status("张三", TRANSCRIPT), "fabricated")

    def test_owner_missing_and_collective_are_not_fabricated(self):
        self.assertEqual(owner_status("", TRANSCRIPT), "missing")
        self.assertEqual(owner_status("未提及", TRANSCRIPT), "missing")
        self.assertEqual(owner_status("全体参会人员", TRANSCRIPT), "missing")
        self.assertEqual(owner_status("待确认", TRANSCRIPT), "missing")
        self.assertEqual(owner_status("徐汇区（执行方未明确）", TRANSCRIPT), "missing")

    def test_deadline_located_by_components(self):
        self.assertEqual(deadline_status("今年8月份", TRANSCRIPT), "located")
        self.assertEqual(deadline_status("未提及", TRANSCRIPT), "missing")
        self.assertEqual(deadline_status("下周五之前", TRANSCRIPT), "unlocated")

    def test_deadline_field_misuse(self):
        self.assertEqual(deadline_status("继续推进密评工作", TRANSCRIPT), "misuse")

    def test_evidence_located_or_unlocated(self):
        quotes = [{"quote": "我们争取在今年的8月份能够全面的上线。"}]
        self.assertEqual(evidence_status(quotes, TRANSCRIPT), "located")
        self.assertEqual(evidence_status([{"quote": "完全不存在的句子"}], TRANSCRIPT), "unlocated")

    def test_validate_item_drop_flags(self):
        item = {
            "content": "推进密评工作",
            "owner": {"text": "张三"},
            "deadline": {"text": "下周五之前"},
            "evidence": [{"quote": "罗鹏主任提出要重视密评工作。"}],
        }
        check = validate_item(item, TRANSCRIPT)
        self.assertTrue(check["drop_owner"])
        self.assertTrue(check["drop_deadline"])
        self.assertEqual(check["evidence_status"], "located")
        self.assertFalse(check["drop_evidence"])


if __name__ == "__main__":
    unittest.main()
