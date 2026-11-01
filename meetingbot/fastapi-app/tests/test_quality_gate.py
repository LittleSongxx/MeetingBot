import unittest

from evaluation.quality_gate import evaluate


class QualityGateTest(unittest.TestCase):
    def test_all_pass(self):
        rows = evaluate("qa", {
            "citation_precision": 0.95,
            "citation_recall": 0.90,
            "found_rate": 1.0,
            "refusal_accuracy": 1.0,
        })
        self.assertTrue(all(row["passed"] for row in rows))

    def test_fail_and_missing(self):
        rows = evaluate("qa", {"citation_precision": 0.80, "citation_recall": 0.90})
        by_name = {row["metric"]: row for row in rows}
        self.assertFalse(by_name["citation_precision"]["passed"])
        self.assertFalse(by_name["found_rate"]["passed"])
        self.assertIsNone(by_name["found_rate"]["value"])

    def test_duplicate_rate_is_max_threshold(self):
        rows = evaluate("scorecard", {"actions": {"duplicate_rate": 0.01}})
        row = [item for item in rows if item["metric"] == "actions.duplicate_rate"][0]
        self.assertTrue(row["passed"])
        rows = evaluate("scorecard", {"actions": {"duplicate_rate": 0.2}})
        row = [item for item in rows if item["metric"] == "actions.duplicate_rate"][0]
        self.assertFalse(row["passed"])


if __name__ == "__main__":
    unittest.main()
