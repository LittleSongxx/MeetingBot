import unittest

from services import coverage


class StateCoverageTest(unittest.TestCase):
    def test_state_aware_off_keeps_legacy_plan(self):
        plan = coverage.build_plan("系列里这个决策确认了吗？", recordings=[6, 7])
        self.assertEqual(plan.required_state_kinds, ())
        self.assertFalse(plan.requires_confirmation_check)

    def test_series_decision_question_requires_state_kinds(self):
        plan = coverage.build_plan(
            "系列里移动端附件下载进度修复这件事，现在是已确认的决策还是未确认的候选？",
            recordings=[6, 7], state_aware=True, series_ids=[1])
        self.assertIn("decision", plan.required_state_kinds)
        self.assertIn("recording", plan.required_state_kinds)  # 系列 → 确认系列范围
        self.assertTrue(plan.requires_confirmation_check)
        self.assertEqual(plan.series_ids, (1,))

    def test_action_item_question_requires_action_item_kind(self):
        plan = coverage.build_plan("系列里还开着的待办有哪些？负责人和截止时间？",
                                   recordings=[6], state_aware=True)
        self.assertIn("action_item", plan.required_state_kinds)
        self.assertIn("owner", plan.required_slots)
        self.assertIn("deadline", plan.required_slots)

    def test_query_meeting_state_marks_kind_and_slots(self):
        plan = coverage.build_plan("系列里待办负责人和截止时间？", state_aware=True)
        state = coverage.EvidenceState(plan=plan)
        coverage.observe_tool_result(state, "query_meeting_state", {
            "ok": True, "kind": "action_item",
            "items": [{"status": "OPEN", "owner_name": "王工",
                       "deadline_text": "8月30日18:00"}],
        })
        self.assertIn("action_item", state.covered_state_kinds)
        self.assertEqual(state.missing_state_kinds.count("action_item"), 0)
        self.assertEqual(state.slot_status.get("owner"), "explicit")
        self.assertEqual(state.slot_status.get("deadline"), "explicit")

    def test_missing_state_kind_gets_hint(self):
        plan = coverage.build_plan("系列里这个决策确认了吗？", state_aware=True,
                                   series_ids=[1])
        state = coverage.EvidenceState(plan=plan)
        coverage.observe_tool_result(state, "search_knowledge", {
            "ok": True, "hits": [{"chunk_type": "TRANSCRIPT_WINDOW", "source_id": 6}]})
        self.assertIn("decision", state.missing_state_kinds)
        self.assertTrue(state.needs_supplement())
        hint = coverage.supplement_hint(state)
        self.assertIn("query_meeting_state", hint)
        self.assertIn("series_id=1", hint)

    def test_get_minutes_resolves_confirmation_check(self):
        plan = coverage.build_plan("这个决定是草稿还是已确认？", state_aware=True)
        state = coverage.EvidenceState(plan=plan)
        coverage.observe_tool_result(state, "get_minutes", {"ok": True, "minutes": [{"id": 1}]})
        self.assertTrue(state.minutes_observed)
        self.assertFalse(state._confirmation_unresolved())

    def test_no_hint_after_attempt_once(self):
        plan = coverage.build_plan("系列里待办有哪些？", state_aware=True)
        state = coverage.EvidenceState(plan=plan, attempts=1)
        self.assertFalse(state.needs_supplement())


if __name__ == "__main__":
    unittest.main()
