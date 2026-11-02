import unittest

from evaluation.validate_golden import validate_multiturn, validate_qa


class GoldenValidationTest(unittest.TestCase):
    def test_qa_valid(self):
        self.assertEqual(validate_qa({"questions": [
            {"id": "q1", "question": "问题", "answerable": True, "expected_segments": [1]},
            {"id": "q2", "question": "问题2", "answerable": False, "expected_segments": []},
        ]}), [])

    def test_qa_invalid(self):
        issues = validate_qa({"questions": [
            {"id": "q1", "question": "问题", "answerable": True, "expected_segments": []},
            {"id": "q1", "question": "", "answerable": "yes", "expected_segments": []},
        ]})
        self.assertTrue(issues)

    def test_multiturn_requires_two_turns(self):
        issues = validate_multiturn({"conversations": [{"id": "c1", "turns": [{"question": "q", "expected_segments": [1]}]}]})
        self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()
