"""意图层零调用评分器（逐类 F1 + 混淆矩阵）的单测：零模型、零网络。

四组断言各自对应一条容易静默出错的性质：
- **没有决策 ≠ 预测为空**：缺产品决策时该题不进分母，也不把它算成 0 分；
- **支持数为 0 的类报 null**：判不了不等于"这类全错"；
- **逐类分列、不给总分**：宏平均只是一列，不许合成"意图得分"；
- **两个标注口径分开**：人工已确认与代理起草各自计数（标签来源如实分层）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from regression import intent_metrics as im  # noqa: E402

TAXONOMY = ["single_fact", "aggregation", "cross_meeting", "procedure", "smalltalk"]


def _qa(cases):
    return {"annotation": {"intent_taxonomy": TAXONOMY}, "cases": cases}


def _decisions(rows):
    return {"decisions": [{"case_id": cid, "intent": intent} for cid, intent in rows]}


class SummarizeTest(unittest.TestCase):
    def test_per_class_f1_and_confusion(self):
        qa = _qa([
            {"id": "q01", "intent": "single_fact", "case_source": "human"},
            {"id": "q02", "intent": "single_fact", "case_source": "human"},
            {"id": "q03", "intent": "aggregation", "case_source": "human"},
            {"id": "q04", "intent": "smalltalk", "case_source": "human"},
        ])
        decisions = _decisions([("q01", "single_fact"), ("q02", "aggregation"),
                                ("q03", "aggregation"), ("q04", "smalltalk")])
        out = im.summarize(decisions, qa)
        self.assertEqual(out["sample"], 4)
        single = out["per_class"]["single_fact"]
        self.assertEqual((single["true_positive"], single["support"], single["predicted"]), (1, 2, 1))
        self.assertEqual(single["precision"], 1.0)
        self.assertEqual(single["recall"], 0.5)
        self.assertAlmostEqual(single["f1"], 2 / 3)
        self.assertEqual(out["confusion"]["single_fact"]["aggregation"], 1)
        self.assertEqual(out["confusion"]["aggregation"]["aggregation"], 1)
        # 逐类分列、没有加权总分
        self.assertNotIn("overall", out)
        self.assertNotIn("score", out)
        self.assertIn("macro_f1", out)

    def test_classes_without_support_report_null_not_zero(self):
        qa = _qa([{"id": "q01", "intent": "single_fact", "case_source": "human"}])
        out = im.summarize(_decisions([("q01", "single_fact")]), qa)
        self.assertIsNone(out["per_class"]["procedure"]["f1"])
        self.assertIsNone(out["per_class"]["procedure"]["precision"])
        self.assertEqual(out["per_class"]["procedure"]["support"], 0)

    def test_missing_decisions_are_not_scored_as_empty(self):
        qa = _qa([
            {"id": "q01", "intent": "single_fact", "case_source": "human"},
            {"id": "q02", "intent": "aggregation", "case_source": "human"},
        ])
        out = im.summarize(_decisions([("q01", "single_fact")]), qa)
        self.assertEqual(out["sample"], 1, "缺决策的题不进分母")
        self.assertEqual([row["case_id"] for row in out["skipped"]], ["q02"])

    def test_human_and_draft_calibers_are_reported_separately(self):
        qa = _qa([
            {"id": "q01", "intent": "single_fact", "case_source": "human"},
            {"id": "q90", "intent": "aggregation", "case_source": "agent_draft"},
        ])
        out = im.summarize(_decisions([("q01", "single_fact"), ("q90", "single_fact")]), qa)
        self.assertEqual((out["sample_verified"], out["sample_draft"]), (1, 1))
        self.assertIn("代理起草", out["reading"], "标签来源要如实分层并写在口径里")

    def test_unregistered_labels_are_reported_not_dropped(self):
        qa = _qa([{"id": "q01", "intent": "single_fact", "case_source": "human"}])
        out = im.summarize(_decisions([("q01", "weather")]), qa)
        self.assertIn("weather", out["per_class"])
        self.assertEqual(out["confusion"]["single_fact"]["weather"], 1)

    def test_no_decisions_reports_no_sample(self):
        out = im.summarize(None, _qa([{"id": "q01", "intent": "single_fact"}]))
        self.assertEqual(out["sample"], 0)
        self.assertIn("无样本", out["reading"])

    def test_loader_tolerates_missing_files(self):
        decisions, qa_set = im.load_inputs(Path(tempfile.gettempdir()) / "nope.json",
                                           Path(tempfile.gettempdir()) / "nope2.json")
        self.assertIsNone(decisions)
        self.assertIsNone(qa_set)


class RealSeedSetTest(unittest.TestCase):
    """真标注集上的形状检查：意图枚举、代理起草的如实分层。"""

    def test_seed_set_carries_intents_and_sources(self):
        qa_set = json.loads((EVALUATION_DIR / "gold" / "qa_seed.json").read_text(encoding="utf-8"))
        taxonomy = qa_set["annotation"]["intent_taxonomy"]
        cases = qa_set["cases"]
        self.assertTrue(all(case.get("intent") in taxonomy for case in cases))
        self.assertTrue(all(case.get("intent_source") for case in cases))
        draft = [case for case in cases if case.get("case_source") == "agent_draft"]
        self.assertTrue(draft, "扩标的那批必须如实标成代理起草")
        self.assertIn("待人工确认", qa_set["annotation"]["intent_note"])


if __name__ == "__main__":
    unittest.main()


class CaptureGateTest(unittest.TestCase):
    """抓取入口的预算闸：缺授权、授权不足、缺 token 都必须拒绝执行。"""

    def test_requires_an_explicit_budget(self):
        from judges import intent
        self.assertEqual(intent.main(["--token", "t"]), 2)

    def test_requires_enough_budget_for_every_case(self):
        from judges import intent
        self.assertEqual(intent.main(["--token", "t", "--authorized-llm-calls", "3"]), 2)

    def test_requires_a_token(self):
        from judges import intent
        self.assertEqual(intent.main(["--authorized-llm-calls", "999"]), 2)

    def test_artifact_records_product_switches(self):
        """开关必须由**被测进程**自报：抓取进程的 env 与容器的 env 是两个空间。"""
        from judges import intent
        artifact = intent._artifact("http://x", {"kb_query_plan_enabled": True},
                                    [{"case_id": "q01", "intent": "single_fact"}])
        self.assertTrue(artifact["captured_from"]["product_switches"]["kb_query_plan_enabled"])
        self.assertIn("captured_at", artifact["captured_from"])
