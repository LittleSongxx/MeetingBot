"""契约层与共享文本/时间判据的单元测试。

这些测试锁的是**不变量**（结构上不该发生的事），不是某条启发式规则的输出。
因此每个用例都对应一句契约陈述，而不是一个调参结果。

运行：
    python -m unittest discover -s tests -p 'test_contracts.py' -v
"""

from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path

from common import textsim, timex
from services import contracts


def minutes(**overrides):
    base = {
        "summary": "会议确定了试点与期限。",
        "topics": [{"title": "试点", "summary": "讨论试点范围。", "origin": "stated",
                    "evidence": [{"quote": "讨论试点范围", "speaker": "程安"}]}],
        "viewpoints": [{"speaker": "程安", "viewpoint": "只在产品组试点。", "origin": "stated",
                        "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}]}],
        "decisions": [{
            "content": "只在产品组试点。", "basis": "程安提出。", "origin": "stated",
            "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}],
            "owner": {"text": "程安", "basis": "stated",
                      "evidence": [{"quote": "程安", "speaker": "程安"}]},
            "deadline": {"text": "2026 年 9 月 28 日", "basis": "stated", "normalized": "2026-09-28",
                         "evidence": [{"quote": "2026 年 9 月 28 日", "speaker": "程安"}]},
        }],
        "pending_items": [],
        "risks": [],
    }
    base.update(overrides)
    return base


TRANSCRIPT = "[程安] 只在产品组试点，试点从 2026 年 9 月 28 日开始，陈悦负责准备样例。"


class TimeSemanticsTest(unittest.TestCase):
    """时间语义的类型判定：结构性区分"断言了一个时点"与"没有断言"。"""

    def test_structured_date_is_stated_with_iso_normalization(self):
        for surface in ("2026年9月28日", "2026-09-28", "2026/9/28"):
            parsed = timex.parse_time(surface)
            self.assertEqual(parsed.basis, "stated", msg=surface)
            self.assertEqual(parsed.normalized, "2026-09-28", msg=surface)

    def test_relative_expression_requires_an_anchor(self):
        self.assertEqual(timex.parse_time("下周三").basis, "open_ended")
        anchored = timex.parse_time("下周三", reference_date="2026-09-25")
        self.assertEqual(anchored.basis, "derived")
        self.assertEqual(anchored.anchor, "2026-09-25")

    def test_duration_is_not_resolved_from_the_meeting_date(self):
        """时长只确立长度；起点未给定时拿会议日期去推就是过度推断。"""
        parsed = timex.parse_time("十五个工作日", reference_date="2026-09-25")
        self.assertEqual(parsed.basis, "open_ended")
        self.assertEqual(parsed.normalized, "")

    def test_open_ended_and_absent_are_distinguished(self):
        for surface in ("年内", "上线后", "持续推进", "每学期"):
            self.assertEqual(timex.parse_time(surface).basis, "open_ended", msg=surface)
        for surface in ("", "   ", "无", "待确认", "待商定", "n/a"):
            self.assertEqual(timex.parse_time(surface).basis, "not_mentioned", msg=surface)

    def test_an_action_phrase_carries_no_time_semantics(self):
        """这是期限字段误用的判据：动作文本不承载时间语义。"""
        for surface in ("完成数据收集和目标差距分析", "确定一门攻坚学科并制定提分计划"):
            self.assertEqual(timex.parse_time(surface).basis, "", msg=surface)
            self.assertFalse(timex.looks_like_a_time(surface), msg=surface)


class SharedTextIdentityTest(unittest.TestCase):
    """条目身份判据必须是共享的，且对改写宽容、对巧合严格。"""

    def test_paraphrase_counts_as_the_same_item(self):
        self.assertTrue(textsim.is_same_item("补充三个入口", "补充三个入口并补充文案"))

    def test_short_coincidence_is_not_the_same_item(self):
        """两字巧合的相似度恰好 0.5，但没有足够长的公共块。"""
        self.assertFalse(textsim.is_same_item("旧事项", "全新的事项"))

    def test_coverage_separates_merge_from_deletion(self):
        pool = textsim.content_grams("建设和运营面向企业和社会服务的人工智能研发平台")
        merged = textsim.content_grams("建设和运营面向企业的人工智能研发平台")
        deleted = textsim.content_grams("完全无关的另一个议题内容")
        self.assertTrue(textsim.is_covered_by(merged, pool))
        self.assertFalse(textsim.is_covered_by(deleted, pool))


class SlotInvariantTest(unittest.TestCase):
    """槽位不变量：按 SLOT_KINDS 表驱动，与具体字段无关。"""

    def test_clean_minutes_pass(self):
        report = contracts.conformance_report(minutes(), TRANSCRIPT)
        self.assertEqual(report["error_count"], 0, msg=report["violations"])

    def test_action_in_a_time_slot_is_a_type_mismatch(self):
        broken = minutes(decisions=[{
            "content": "确定一门攻坚学科。", "basis": "会上讨论。", "origin": "model_inferred",
            "evidence": [],
            "owner": {"text": "待确认", "basis": "not_mentioned"},
            "deadline": {"text": "确定一门攻坚学科并制定提分计划", "basis": "stated",
                         "evidence": [{"quote": "确定一门攻坚学科", "speaker": ""}]},
        }])
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("slot_type_mismatch", kinds)

    def test_assertion_without_locatable_evidence_is_a_violation(self):
        """槽位级：声明 stated 却给不出能在原文定位的证据。"""
        broken = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。", "origin": "stated",
            "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}],
            "owner": {"text": "王富贵", "basis": "stated",
                      "evidence": [{"quote": "这句话在原文里根本不存在", "speaker": ""}]},
            "deadline": {"text": "无", "basis": "not_mentioned"},
        }])
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("stated_without_verifiable_evidence", kinds)

    def test_evidence_on_a_non_assertion_is_a_violation(self):
        """伪造引用可判定：没有断言的地方不该有证据。"""
        broken = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。", "origin": "stated",
            "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}],
            "owner": {"text": "程安", "basis": "stated",
                      "evidence": [{"quote": "程安", "speaker": "程安"}]},
            "deadline": {"text": "无", "basis": "not_mentioned",
                         "evidence": [{"quote": "程安", "speaker": "程安"}]},
        }])
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("evidence_on_non_assertion", kinds)

    def test_derived_without_anchor_is_a_violation(self):
        broken = minutes(pending_items=[{
            "content": "准备样例。", "origin": "model_inferred", "evidence": [],
            "owner": {"text": "待确认", "basis": "not_mentioned"},
            "deadline": {"text": "下周三", "basis": "derived"},
        }])
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("derived_without_anchor", kinds)

    def test_evidence_locatability_is_public_and_shared_with_the_metric_suite(self):
        """评测套件要报「证据可定位率」，产品要判来源闭合——必须是同一个判据。

        用同一个 quote 走两条路：公开出口与整份校验，二者结论必须一致。不一致就说明
        又出现"两套规则"，那正是本轮下沉契约要消灭的东西。
        """
        source = contracts.normalize_source(TRANSCRIPT)
        for quote, expected in (
            ("只在产品组试点", True),
            ("这句话在原文里根本不存在", False),
            ("  只在产品组试点。 ", True),      # 空白与标点不参与定位
            ("试点", True),                      # 恰好 2 字：边界之内的最短可定位引用
            ("试", False),                       # 归一后不足 2 字，一律不算可定位
            ("", False),
        ):
            self.assertEqual(contracts.evidence_locatable(quote, source), expected, msg=quote)

        # 产品判定的方向必须与公开出口一致：定位不到的引用 → stated 却无可用证据
        broken = minutes(decisions=[{
            "content": "只在产品组试点。", "basis": "程安提出。", "origin": "stated",
            "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}],
            "owner": {"text": "程安", "basis": "stated",
                      "evidence": [{"quote": "这句话在原文里根本不存在", "speaker": ""}]},
            "deadline": {"text": "无", "basis": "not_mentioned"},
        }])
        self.assertFalse(contracts.evidence_locatable("这句话在原文里根本不存在", source))
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("stated_without_verifiable_evidence", kinds)

    def test_entity_slot_rejects_open_ended(self):
        broken = minutes(pending_items=[{
            "content": "准备样例。", "origin": "model_inferred", "evidence": [],
            "owner": {"text": "年内", "basis": "open_ended"},
            "deadline": {"text": "无", "basis": "not_mentioned"},
        }])
        kinds = {v["kind"] for v in contracts.conformance_report(broken, TRANSCRIPT)["violations"]}
        self.assertIn("slot_type_mismatch", kinds)


class ContentEmptyRewriteTest(unittest.TestCase):
    """内容整体归零必须被拦住：默认值会把「模型没给」变成「模型给了空的」。"""

    def _draft(self, items: int = 6):
        return {
            "summary": "会议确定了试点范围与上线安排。",
            "topics": [{"title": f"议题{i}", "summary": f"讨论内容{i}。"} for i in range(items)],
            "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
        }

    def test_draft_with_items_rewritten_to_nothing_is_flagged(self):
        revised = {"summary": "本次会议围绕试点展开。", "topics": [], "viewpoints": [],
                   "decisions": [], "pending_items": [], "risks": []}
        self.assertTrue(contracts.is_content_empty_rewrite(self._draft(), revised))

    def test_the_real_incident_shape_is_flagged(self):
        """复现 vcsum_21 的形态：模型只返回 summary，验证器把五个列表补成空数组。"""
        _, MinutesResult = contracts.minutes_pydantic_models()
        raw = {"summary": "本次会议为一场线上公益主题对话。"}
        coerced = MinutesResult.model_validate(raw).model_dump()  # 默认值让它"合法"
        self.assertTrue(contracts.is_content_empty_rewrite(self._draft(63), coerced))

    def test_a_genuinely_short_draft_is_not_flagged(self):
        """初稿本来就没什么内容时，空改写稿不算异常——否则会误伤短会议。"""
        revised = {"summary": "无实质内容。", "topics": [], "viewpoints": [],
                   "decisions": [], "pending_items": [], "risks": []}
        self.assertFalse(contracts.is_content_empty_rewrite(self._draft(items=2), revised))

    def test_a_normal_rewrite_is_not_flagged(self):
        draft = self._draft()
        revised = dict(draft)
        revised["topics"] = draft["topics"][:5]
        self.assertFalse(contracts.is_content_empty_rewrite(draft, revised))

    def test_clearing_one_field_is_not_degeneracy(self):
        """单个字段合法清空（例如会议确实没有决策）不该被当成归零。"""
        draft = {
            "summary": "会议讨论了三项议题。",
            "topics": [{"title": f"议题{i}", "summary": "内容。"} for i in range(6)],
            "viewpoints": [{"speaker": "程安", "viewpoint": "只在产品组试点。"}],
            "decisions": [], "pending_items": [], "risks": [],
        }
        revised = dict(draft)
        revised["viewpoints"] = []
        self.assertFalse(contracts.is_content_empty_rewrite(draft, revised))


class UnprovidedContentGenerationTest(unittest.TestCase):
    """单分块生成路径的护栏：读**根因**（模型没给字段），不是再找一个内容代理。

    这条路径此前完全没有护栏——`is_content_empty_rewrite` 需要参照物，而单分块
    没有参照物。事故形态是"模型只返回 summary，五个列表字段由默认值补成空"。
    """

    def _model(self):
        _, MinutesResult = contracts.minutes_pydantic_models()
        return MinutesResult

    def test_the_incident_shape_is_flagged_on_the_generation_side(self):
        """复现 vcsum_21：模型只给了 summary，五个列表字段压根没出现。"""
        MinutesResult = self._model()
        result = MinutesResult.model_validate({"summary": "本次会议为一场线上公益主题对话。"})
        self.assertEqual(contracts.unprovided_content_fields(result),
                         ("topics", "viewpoints", "decisions", "pending_items", "risks"))
        self.assertTrue(contracts.is_unprovided_content_generation(result))

    def test_a_field_explicitly_given_as_empty_is_not_flagged(self):
        """模型**显式给了空数组**与"没给"必须分得开：前者合法，会议确实可以没有决策。
        默认值把这两件事混成一件，这里要能拆开。"""
        MinutesResult = self._model()
        result = MinutesResult.model_validate({
            "summary": "本次会议为一场线上公益主题对话。",
            "topics": [], "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
        })
        self.assertEqual(contracts.unprovided_content_fields(result), ())
        self.assertFalse(contracts.is_unprovided_content_generation(result))

    def test_partially_omitted_fields_are_not_treated_as_degeneracy(self):
        """只缺一两个字段不按失败处理：那不是事故形态，误伤会直接拉低可靠性。"""
        MinutesResult = self._model()
        result = MinutesResult.model_validate({
            "summary": "会议围绕试点展开。",
            "topics": [{"title": "试点", "summary": "讨论试点范围。"}],
        })
        self.assertIn("risks", contracts.unprovided_content_fields(result))
        self.assertFalse(contracts.is_unprovided_content_generation(result))

    def test_a_complete_output_has_nothing_unprovided(self):
        MinutesResult = self._model()
        result = MinutesResult.model_validate({
            "summary": "会议确定了试点范围。",
            "topics": [{"title": "试点", "summary": "讨论试点范围。"}],
            "viewpoints": [], "decisions": [], "pending_items": [], "risks": [],
        })
        self.assertEqual(contracts.unprovided_content_fields(result), ())
        self.assertFalse(contracts.is_unprovided_content_generation(result))

    def test_the_recorded_incident_output_is_caught_through_the_real_parse_chain(self):
        """用**真实事故产物**跑一遍完整解析链，而不是手工构造一个同形状的字典。

        护栏能不能生效取决于整条链上没人偷偷补默认值：模型文本 → `parse_model_json`
        → `MinutesResult.model_validate`。其中 `parse_model_json` 走的是 `SchemaJsonParser`
        （继承 `JsonOutputParser`，只多剥代码块围栏、**不补字段**）。这条链一旦有人加了
        "缺失字段填空数组"，护栏就会变成**永远不触发的静默零**——那正是它要防的形态本身。

        语料：`vcsum_21` 那次退化的 `revised.raw_model_output`，实测只有 `summary`
        （627 字、五个列表字段一条没有），初稿有 63 条。
        """
        artifact = (Path(__file__).resolve().parents[3] / "evaluation" / "public_benchmark"
                    / "results" / "vcsum-20260925T140012Z-2edf738c" / "vcsum_21.json")
        if not artifact.exists():
            self.skipTest(f"事故产物不在本工作区：{artifact}")
        import json as _json

        from common.model_json import parse_model_json
        raw = _json.loads(artifact.read_text(encoding="utf-8"))["revised"]["raw_model_output"]
        self.assertEqual(sorted(raw.keys()), ["summary"], "事故形态：模型只返回 summary")
        parsed = parse_model_json(_json.dumps(raw, ensure_ascii=False))
        self.assertEqual(sorted(parsed.keys()), ["summary"], "解析链不得补默认值")
        MinutesResult = self._model()
        result = MinutesResult.model_validate(parsed)
        self.assertEqual(contracts.unprovided_content_fields(result),
                         ("topics", "viewpoints", "decisions", "pending_items", "risks"))
        self.assertTrue(contracts.is_unprovided_content_generation(result))
        self.assertEqual(contracts.list_item_count(result.model_dump()), 0)

    def test_objects_without_field_tracking_are_not_flagged(self):
        """拿不到 `model_fields_set` 的对象（例如普通 dict）不能因此被判失败：
        这一条只在**确证缺席**时成立，判不出来不得误伤——宁可漏拦一次，
        也不要因为读不到字段信息就把正常纪要判成失败。"""
        self.assertEqual(contracts.unprovided_content_fields({"summary": "x"}), ())
        self.assertFalse(contracts.is_unprovided_content_generation({"summary": "x"}))


class IssueEvidenceGateTest(unittest.TestCase):
    """审查意见的证据门控：一张策略表覆盖六类规则。"""

    def test_missing_rule_without_premise_is_rejected(self):
        violations = contracts.check_issue_evidence(
            [{"issue_type": "MISSING_DEADLINE", "source_span": "", "target_text": ""}],
            TRANSCRIPT,
        )
        self.assertEqual([v.kind for v in violations], ["missing_rule_without_premise"])

    def test_missing_rule_with_unlocatable_premise_is_rejected(self):
        violations = contracts.check_issue_evidence(
            [{"issue_type": "MISSING_OWNER", "source_span": "原文里没有这句", "target_text": ""}],
            TRANSCRIPT,
        )
        self.assertEqual([v.kind for v in violations], ["missing_rule_with_unsupported_premise"])

    def test_missing_rule_with_a_real_premise_passes(self):
        violations = contracts.check_issue_evidence(
            [{"issue_type": "MISSING_DEADLINE", "source_span": "2026 年 9 月 28 日", "target_text": ""}],
            TRANSCRIPT,
        )
        self.assertEqual(violations, [])

    def test_unsupported_rule_whose_target_is_present_is_rejected(self):
        violations = contracts.check_issue_evidence(
            [{"issue_type": "UNSUPPORTED", "source_span": "", "target_text": "只在产品组试点"}],
            TRANSCRIPT,
        )
        self.assertEqual([v.kind for v in violations], ["unsupported_claim_present_in_source"])

    def test_quality_rules_need_no_evidence(self):
        for issue_type in ("NOT_ACTIONABLE", "VAGUE"):
            violations = contracts.check_issue_evidence(
                [{"issue_type": issue_type, "source_span": "", "target_text": ""}],
                TRANSCRIPT,
            )
            self.assertEqual(violations, [], msg=issue_type)

    def test_policy_table_covers_every_declared_issue_type(self):
        schema_types = set(
            contracts.review_json_schema()["properties"]["issues"]["items"]["properties"]["issue_type"]["enum"]
        )
        self.assertEqual(schema_types, set(contracts.ISSUE_EVIDENCE_POLICY))

    def test_evidence_guide_is_derived_from_the_policy_table(self):
        guide = contracts.issue_evidence_guide()
        for issue_type in contracts.ISSUE_EVIDENCE_POLICY:
            self.assertIn(issue_type, guide)


class DispositionInvariantTest(unittest.TestCase):
    """静默删除在结构上不可能：消失的条目必须有声明 + 理由 + 证据。"""

    BASE = {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
            "decisions": [{"content": "甲事项"}, {"content": "乙事项"}],
            "pending_items": []}

    def test_silent_deletion_is_detected(self):
        refined = {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
                   "decisions": [{"content": "甲事项"}], "pending_items": []}
        violations = contracts.validate_dispositions(self.BASE, refined, [], 0)
        self.assertEqual([v.kind for v in violations], ["silent_deletion"])

    def test_declared_deletion_with_reason_and_evidence_passes(self):
        refined = {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
                   "decisions": [{"content": "甲事项"}], "pending_items": []}
        dispositions = [{
            "op": "delete", "field": "decisions", "target_text": "乙事项",
            "reason_issue_index": 0, "evidence": [{"quote": "乙"}],
        }]
        violations = contracts.validate_dispositions(self.BASE, refined, dispositions, 1)
        self.assertEqual(violations, [])

    def test_deletion_without_reason_or_evidence_is_unjustified(self):
        refined = {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
                   "decisions": [{"content": "甲事项"}], "pending_items": []}
        violations = contracts.validate_dispositions(
            self.BASE, refined, [{"op": "delete", "field": "decisions", "target_text": "乙事项"}], 1,
        )
        kinds = {v.kind for v in violations}
        self.assertIn("unjustified_deletion", kinds)

    def test_rewording_is_not_a_deletion(self):
        """改写不算消失——这正是"用文本相等判身份"会犯的错。"""
        refined = {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
                   "decisions": [{"content": "甲事项"}, {"content": "关于乙事项的进一步说明"}],
                   "pending_items": []}
        self.assertEqual(contracts.validate_dispositions(self.BASE, refined, [], 0), [])

    def test_identical_documents_never_violate(self):
        self.assertEqual(contracts.validate_dispositions(self.BASE, self.BASE, [], 0), [])


class ContractDerivationTest(unittest.TestCase):
    """模型约束、校验器与展示三者同源——由契约表派生，不可能漂移。"""

    def test_minutes_schema_is_derived_from_the_slot_table(self):
        schema = contracts.minutes_json_schema()
        for field_name, slots in contracts.SLOT_KINDS.items():
            props = schema["properties"][field_name]["items"]["properties"]
            for slot in slots:
                self.assertIn(slot, props, msg=f"{field_name}.{slot}")
        for field_name in contracts.LIST_FIELDS:
            props = schema["properties"][field_name]["items"]["properties"]
            self.assertIn("origin", props)
            self.assertIn("evidence", props)

    def test_pydantic_models_accept_a_contract_shaped_document(self):
        _, root = contracts.minutes_pydantic_models()
        instance = root.model_validate(minutes())
        self.assertEqual(instance.decisions[0].deadline.basis, "stated")

    def test_slot_table_covers_every_list_field_that_has_a_typed_slot(self):
        for field_name in contracts.SLOT_KINDS:
            self.assertIn(field_name, contracts.LIST_FIELDS)


class LegacyCompatibilityTest(unittest.TestCase):
    """旧数据形态必须能读，且展示回退后消费方无感。"""

    LEGACY = {
        "summary": "s",
        "topics": [], "viewpoints": [], "risks": [],
        "decisions": [{"content": "c", "basis": "b", "owner_suggestion": "",
                       "deadline_suggestion": "无"}],
        "pending_items": [{"content": "p", "owner_suggestion": "相关合作方",
                           "deadline_suggestion": "2026年9月28日"}],
    }

    def test_upgrade_is_idempotent_and_types_the_slots(self):
        first = contracts.upgrade_output(self.LEGACY)
        second = contracts.upgrade_output(first)
        self.assertEqual(
            first["pending_items"][0]["deadline"]["normalized"],
            second["pending_items"][0]["deadline"]["normalized"],
        )
        self.assertEqual(first["decisions"][0]["deadline"]["basis"], "not_mentioned")
        self.assertEqual(first["pending_items"][0]["deadline"]["basis"], "stated")

    def test_presentation_preserves_the_consumer_string_fields(self):
        presented = contracts.present_output(contracts.upgrade_output(self.LEGACY))
        self.assertEqual(presented["pending_items"][0]["owner_suggestion"], "相关合作方")
        self.assertEqual(presented["decisions"][0]["deadline_suggestion"], "未提及")

    def test_legacy_field_misuse_survives_upgrade_and_is_flagged(self):
        """旧数据里"把动作写进时间槽"的值必须保留并被判为类型不符，而不是被静默丢弃。"""
        legacy = dict(self.LEGACY)
        legacy["pending_items"] = [{"content": "p", "owner_suggestion": "待确认",
                                    "deadline_suggestion": "完成数据收集和目标差距分析"}]
        upgraded = contracts.upgrade_output(legacy)
        self.assertEqual(upgraded["pending_items"][0]["deadline"]["text"], "完成数据收集和目标差距分析")
        kinds = {v.kind for v in contracts.validate_minutes(upgraded, "无关转写")}
        self.assertIn("slot_type_mismatch", kinds)

    def test_presentation_renders_each_basis_readably(self):
        self.assertEqual(timex.render_time_value({"basis": "not_mentioned"}), "未提及")
        self.assertEqual(timex.render_time_value({"basis": "open_ended", "text": "年内"}), "年内")
        self.assertEqual(
            timex.render_time_value({"basis": "derived", "text": "下周三", "normalized": "2026-10-02"}),
            "下周三（2026-10-02）",
        )
        self.assertIn("模型建议", timex.render_time_value({"basis": "model_inferred", "text": "3月"}))
        self.assertEqual(timex.render_time_value("旧格式字符串"), "旧格式字符串")


class JsonSyntaxRepairTest(unittest.TestCase):
    """大模型 JSON 的常见语法笔误必须被有界修复且可记录。

    实测：一次 AGENT_REVIEW 的响应完整、括号平衡，只因**尾随逗号**被严格解析器拒绝，
    整个审查步骤失败。这类修复只删逗号、不丢弃内容，因此必须与结构抢救区分开报告。
    """

    def test_trailing_comma_is_repaired_without_dropping_content(self):
        from common import model_json

        broken = '{"passed": true, "score": 95, "issues": [], "conclusion": "无遗漏。", }'
        with self.assertRaises(Exception):
            import json as _json
            _json.loads(broken)
        parsed = model_json.parse_model_json(broken)
        self.assertEqual(parsed["passed"], True)
        self.assertEqual(parsed["score"], 95)
        self.assertTrue(model_json.LAST_SALVAGE["repaired"])
        self.assertEqual(model_json.LAST_SALVAGE["dropped_chars"], 0)
        self.assertIn("removed_trailing_comma", model_json.LAST_SALVAGE["actions"])

    def test_double_comma_is_repaired(self):
        from common import model_json

        parsed = model_json.parse_model_json('{"a": 1,, "b": 2}')
        self.assertEqual(parsed, {"a": 1, "b": 2})
        self.assertIn("collapsed_double_comma", model_json.LAST_SALVAGE["actions"])

    def test_valid_json_is_untouched(self):
        from common import model_json

        self.assertEqual(model_json.parse_model_json('{"a": 1}'), {"a": 1})
        self.assertFalse(model_json.LAST_SALVAGE["repaired"])


class DanglingFieldsRepairTest(unittest.TestCase):
    """提前闭括号 + 悬挂字段必须被有界合并且可记录。

    实测（2026-10-05，vcsum_221 的 AGENT_REVIEW）：模型把根对象提前闭合，
    再把 conclusion 以 `, "conclusion": ...` 续在后面、最末补一个闭括号。
    内容一个字符不少，严格解析却报 Extra data，整次自检因此失败。
    修复只删那一个提前闭合的括号，不做任何语义猜测。
    """

    def test_dangling_fields_are_merged_without_dropping_content(self):
        from common import model_json

        broken = ('{"passed": false, "score": 79, "issues": [{"field": "decisions", '
                  '"issue_type": "NOT_ACTIONABLE"}]} , "conclusion": "其余部分无需修改。"}')
        parsed = model_json.parse_model_json(broken)
        self.assertEqual(parsed["passed"], False)
        self.assertEqual(len(parsed["issues"]), 1)
        self.assertEqual(parsed["conclusion"], "其余部分无需修改。")
        self.assertTrue(model_json.LAST_SALVAGE["repaired"])
        self.assertEqual(model_json.LAST_SALVAGE["dropped_chars"], 0)
        self.assertIn("merged_dangling_fields", model_json.LAST_SALVAGE["actions"])

    def test_two_independent_objects_are_not_merged(self):
        from common import model_json

        _fixed, actions = model_json.repair_dangling_fields('{"a": 1}{"b": 2}')
        self.assertEqual(actions, [])

    def test_trailing_prose_is_not_repaired(self):
        from common import model_json

        _fixed, actions = model_json.repair_dangling_fields('{"a": 1} 多余的解释')
        self.assertEqual(actions, [])

    def test_still_invalid_after_join_is_not_repaired(self):
        from common import model_json

        _fixed, actions = model_json.repair_dangling_fields('{"a": 1}, "b": 不完整')
        self.assertEqual(actions, [])

    def test_first_value_must_be_a_dict(self):
        from common import model_json

        _fixed, actions = model_json.repair_dangling_fields('[1, 2], "b": 3}')
        self.assertEqual(actions, [])

    def test_stray_bracket_before_dangling_field_is_tolerated(self):
        """形态二（vcsum_72）：闭括号后多一个游离 `]`，悬挂字段仍应无损失合并。"""
        from common import model_json

        broken = ('{"summary": "s", "topics": []}] , '
                  '"dispositions": [{"field": "topics", "op": "replace"}]}')
        parsed = model_json.parse_model_json(broken)
        self.assertEqual(parsed["summary"], "s")
        self.assertEqual(len(parsed["dispositions"]), 1)
        self.assertIn("merged_dangling_fields", model_json.LAST_SALVAGE["actions"])
        self.assertIn("dropped_stray_bracket", model_json.LAST_SALVAGE["actions"])
        self.assertEqual(model_json.LAST_SALVAGE["dropped_chars"], 0)

    def test_two_stray_brackets_are_beyond_bounds(self):
        """游离闭括号最多容忍一个；两个就超出有界范围，不猜。"""
        from common import model_json

        _fixed, actions = model_json.repair_dangling_fields('{"a": 1}]], "b": "x"}')
        self.assertEqual(actions, [])

    def test_repair_does_not_invent_content(self):
        """修复只删多余逗号，绝不添加或改写任何字段。"""
        from common import model_json

        model_json.parse_model_json('{"keep": "原值", "list": [1, 2, ], }')
        self.assertEqual(model_json.LAST_SALVAGE["dropped_chars"], 0)

    def test_list_field_coercion_accepts_empty_string_for_empty_array(self):
        """实测：`evidence: ""` 被 Pydantic 拒绝，毁掉整份 11k 字纪要。"""
        from services.contracts import minutes_pydantic_models

        _, root = minutes_pydantic_models()
        instance = root.model_validate({
            "summary": "s", "topics": [], "viewpoints": [], "decisions": [], "risks": [],
            "pending_items": [{"content": "c", "origin": "stated", "evidence": ""}],
        })
        self.assertEqual(instance.pending_items[0].evidence, [])


class PreservationGuaranteeTest(unittest.TestCase):
    """保默认必须是结构性保证，不能只是一句提示词。

    实测背景：静默删除约 3 条/场，与旧配置持平——因为此前只做了测量，产品侧 refine
    仍重写全文，没有机制强制保留。`enforce_preservation` 补上那个机制。
    """

    BASE = {"summary": "s", "topics": [], "viewpoints": [], "risks": [], "pending_items": [],
            "decisions": [{"content": "甲事项"}, {"content": "乙事项"}, {"content": "丙事项"}]}

    def _refined(self, contents):
        return {"summary": "s", "topics": [], "viewpoints": [], "risks": [],
                "pending_items": [], "decisions": [{"content": c} for c in contents]}

    def test_undeclared_disappearance_is_restored(self):
        out, report = contracts.enforce_preservation(self.BASE, self._refined(["甲事项"]), None, transcript="")
        self.assertEqual([d["content"] for d in out["decisions"]], ["甲事项", "乙事项", "丙事项"])
        self.assertEqual(report["restored_count"], 2)

    def test_declared_deletion_is_respected(self):
        dispositions = [{"op": "delete", "field": "decisions", "target_text": "乙事项",
                         "reason_issue_index": 0, "evidence": [{"quote": "乙"}]}]
        out, report = contracts.enforce_preservation(
            self.BASE, self._refined(["甲事项"]), dispositions, transcript=""
        )
        self.assertEqual([d["content"] for d in out["decisions"]], ["甲事项", "丙事项"])
        self.assertEqual(report["restored_count"], 1)

    def test_rewording_is_not_a_disappearance(self):
        out, report = contracts.enforce_preservation(
            self.BASE, self._refined(["关于甲事项的进一步说明", "乙事项", "丙事项"]), None, transcript=""
        )
        self.assertEqual(report["restored_count"], 0)

    def test_merge_is_not_a_disappearance(self):
        """措辞被合并吸收进别的条目，不算消失。"""
        out, report = contracts.enforce_preservation(
            self.BASE,
            self._refined(["甲事项与乙事项合并说明", "丙事项"]),
            None,
            transcript="",
        )
        self.assertLessEqual(report["restored_count"], 3)

    def test_restoration_is_idempotent(self):
        once, _ = contracts.enforce_preservation(self.BASE, self._refined(["甲事项"]), None, transcript="")
        twice, report = contracts.enforce_preservation(self.BASE, once, None, transcript="")
        self.assertEqual(report["restored_count"], 0)
        self.assertEqual(len(twice["decisions"]), 3)

    def test_refine_schema_extends_minutes_schema_with_dispositions(self):
        base = contracts.minutes_json_schema()
        refine = contracts.refine_json_schema()
        self.assertNotIn("dispositions", base["required"])
        self.assertIn("dispositions", refine["required"])
        # 纪要本体部分不得被改动
        for key in base["required"]:
            self.assertIn(key, refine["required"])
            self.assertEqual(base["properties"][key], refine["properties"][key])


class EvidenceGateTest(unittest.TestCase):
    """证据门控是**硬拦**：没有前提的主观意见不许推动重写。

    实测依据（2026-09-25/26）：门控只记录不拦时，删除不按依据筛选（被删条目被判
    有依据 41%，比保留下来的 25% 还高），而独立模型族在业界口径下只认可 25% 的
    `UNSUPPORTED` 意见。
    """

    TRANSCRIPT = "程安：我们只在产品组试点，下个月启动，陈悦负责准备样例。"

    def test_unsupported_issue_without_target_is_dropped(self):
        gate = contracts.gate_issues_by_evidence(
            [{"issue_type": "UNSUPPORTED", "field": "topics", "index": 0, "detail": "感觉没依据"}],
            self.TRANSCRIPT)
        self.assertEqual(gate["kept"], [])
        self.assertEqual(gate["by_kind"], {"unsupported_rule_without_target": 1})

    def test_unsupported_issue_whose_target_is_in_the_source_is_dropped(self):
        """判定可能错：被判无依据的文本其实能在原文定位。"""
        gate = contracts.gate_issues_by_evidence(
            [{"issue_type": "UNSUPPORTED", "field": "topics", "index": 0,
              "target_text": "只在产品组试点"}],
            self.TRANSCRIPT)
        self.assertEqual(gate["kept"], [])
        self.assertEqual(gate["by_kind"], {"unsupported_claim_present_in_source": 1})

    def test_unsupported_issue_with_a_genuinely_absent_target_is_kept(self):
        gate = contracts.gate_issues_by_evidence(
            [{"issue_type": "UNSUPPORTED", "field": "topics", "index": 0,
              "target_text": "将在全国范围推广"}],
            self.TRANSCRIPT)
        self.assertEqual(len(gate["kept"]), 1)
        self.assertEqual(gate["by_kind"], {})

    def test_missing_rule_must_prove_the_premise_exists(self):
        without = contracts.gate_issues_by_evidence(
            [{"issue_type": "MISSING_ITEM", "field": "decisions", "index": -1}], self.TRANSCRIPT)
        self.assertEqual(without["kept"], [])
        self.assertIn("missing_rule_without_premise", without["by_kind"])

        with_premise = contracts.gate_issues_by_evidence(
            [{"issue_type": "MISSING_ITEM", "field": "decisions", "index": -1,
              "source_span": "陈悦负责准备样例"}], self.TRANSCRIPT)
        self.assertEqual(len(with_premise["kept"]), 1)

    def test_dropped_issues_are_preserved_not_silently_discarded(self):
        """被挡下的意见必须留在产物里：静默丢弃等于把"审查提过"这件事抹掉。"""
        issue = {"issue_type": "UNSUPPORTED", "field": "topics", "index": 0, "detail": "原始意见文本"}
        gate = contracts.gate_issues_by_evidence([issue], self.TRANSCRIPT)
        self.assertEqual(len(gate["dropped"]), 1)
        self.assertEqual(gate["dropped"][0]["issue"]["detail"], "原始意见文本")
        self.assertTrue(gate["dropped"][0]["detail"])

    def test_report_and_gate_share_one_implementation(self):
        """`check_issue_evidence`（报告）与 `gate_issues_by_evidence`（硬拦）必须同源。"""
        issues = [
            {"issue_type": "UNSUPPORTED", "field": "topics", "index": 0},                    # 无 target
            {"issue_type": "UNSUPPORTED", "field": "topics", "index": 1,
             "target_text": "将在全国范围推广"},                                              # 合法
            {"issue_type": "MISSING_ITEM", "field": "decisions", "index": -1},               # 无 premise
        ]
        violations = contracts.check_issue_evidence(issues, self.TRANSCRIPT)
        gate = contracts.gate_issues_by_evidence(issues, self.TRANSCRIPT)
        self.assertEqual(len(violations), 2)                       # 两条违规
        self.assertEqual(len(gate["kept"]), 1)                     # 一条留下
        self.assertEqual(gate["checked"], 3)
        self.assertEqual(sum(gate["by_kind"].values()), len(violations))


class EvidenceGatedRetentionTest(unittest.TestCase):
    """保留判据看**条目自己的证据**：有依据的恢复，无依据的准许丢弃。

    实测依据：契约 v2 那批 80 条消失条目里 77 条（96%）带可定位证据——
    也就是说被删的绝大多数是有依据的内容。全盘恢复会让产品删不掉无依据内容；
    全按模型声明又会因模型极少声明（25/26 场）而几乎删不动。
    """

    TRANSCRIPT = "程安：我们只在产品组试点，下个月启动，陈悦负责准备样例。"

    def _draft(self):
        return {
            "summary": "会议确定了试点范围。",
            "topics": [{"title": "试点范围", "summary": "只在产品组试点。", "origin": "stated",
                        "evidence": [{"quote": "只在产品组试点", "speaker": "程安"}]}],
            "viewpoints": [{"speaker": "程安", "viewpoint": "应该扩大范围。", "origin": "stated",
                            "evidence": [{"quote": "这句话在转写里根本不存在", "speaker": "程安"}]}],
            "decisions": [], "pending_items": [], "risks": [],
        }

    def _rewrite_without_them(self):
        return {"summary": "会议确定了试点范围。", "topics": [], "viewpoints": [],
                "decisions": [], "pending_items": [], "risks": []}

    def test_grounded_item_is_restored_and_ungrounded_item_is_allowed_to_go(self):
        revised, report = contracts.enforce_preservation(
            self._draft(), self._rewrite_without_them(), [], transcript=self.TRANSCRIPT)
        titles = [item["title"] for item in revised["topics"]]
        self.assertIn("试点范围", titles)                       # 有依据 → 恢复
        self.assertEqual(revised["viewpoints"], [])              # 无依据 → 准许丢弃
        self.assertEqual(report["restored_count"], 1)
        self.assertEqual(report["dropped_ungrounded_count"], 1)
        self.assertEqual(report["dropped_ungrounded"][0]["field"], "viewpoints")

    def test_without_a_transcript_the_old_semantics_hold(self):
        """转写给空串时退回"一律恢复"，无转写可用的调用点语义不变。"""
        revised, report = contracts.enforce_preservation(
            self._draft(), self._rewrite_without_them(), [], transcript="")
        self.assertEqual(report["restored_count"], 2)
        self.assertEqual(report["dropped_ungrounded_count"], 0)

    def test_declared_delete_is_respected_even_for_grounded_items(self):
        dispositions = [{"field": "topics", "op": "delete", "target_text": "试点范围",
                         "reason_issue_index": 0}]
        revised, report = contracts.enforce_preservation(
            self._draft(), self._rewrite_without_them(), dispositions, transcript=self.TRANSCRIPT)
        self.assertEqual(revised["topics"], [])
        self.assertEqual(report["restored_count"], 0)

    def test_the_page_visible_summary_counts_both_directions(self):
        """两个方向都要可观测：恢复了几条有依据的、准许丢了几条无依据的。"""
        _, report = contracts.enforce_preservation(
            self._draft(), self._rewrite_without_them(), [], transcript=self.TRANSCRIPT)
        self.assertIn("恢复", report["note"])
        self.assertIn("丢弃", report["note"])


