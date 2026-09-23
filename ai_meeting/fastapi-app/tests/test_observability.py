import unittest
from datetime import datetime, timedelta, timezone

from tortoise import Tortoise

from common.exception_handler import CustomException
from models import (
    AgentRun,
    AgentStep,
    AiCallLog,
    AiModelConfig,
    MeetingMinutes,
    TranscriptionTask,
    User,
)
from services.observability_service import build_observability, token_usage


def date_range() -> tuple[str, str]:
    """返回覆盖昨天到明天的日期范围，避免跨零点时统计边界把数据切掉。"""
    today = datetime.now().date()
    return (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()


class ObservabilityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.admin = await User.create(username="observe_admin", password="123456", name="Administrator", role="ADMIN")
        self.employee = await User.create(username="observe_employee", password="123456", name="Alice", role="EMPLOYEE")
        self.other = await User.create(username="observe_other", password="123456", name="Bob", role="EMPLOYEE")
        self.agent_config = await AiModelConfig.create(name="Agent", model_type="AGENT", base_url="https://api.openai.com/v1", api_key="key", model_name="agent-model")
        self.minutes_config = await AiModelConfig.create(name="Minutes", model_type="MINUTES", base_url="https://api.openai.com/v1", api_key="key", model_name="minutes-model")

        transcription = await TranscriptionTask.create(meeting_id=1, material_id=1, model_config_id=self.minutes_config.id, initiator_id=self.employee.id)
        other_transcription = await TranscriptionTask.create(meeting_id=2, material_id=2, model_config_id=self.minutes_config.id, initiator_id=self.other.id)
        minutes = await MeetingMinutes.create(meeting_id=1, source_task_id=transcription.id, model_config_id=self.minutes_config.id, created_by=self.employee.id)
        now = datetime.now(timezone.utc)
        self.run = await AgentRun.create(
            run_no="AGR-OBSERVE-001", user_id=self.employee.id, minutes_id=minutes.id, meeting_id=1, model_config_id=self.agent_config.id,
            current_round=2, score=90, revised_json={"summary": "revised"},
            status="SUCCEEDED", final_result="Done", finished_at=now + timedelta(seconds=2),
        )
        await AgentRun.create(
            run_no="AGR-OBSERVE-002", user_id=self.other.id, minutes_id=minutes.id, meeting_id=2, model_config_id=self.agent_config.id,
            status="FAILED", error_message="Other failure", finished_at=now + timedelta(seconds=1),
        )

        log_rows = [
            ("CALL-1", "TRANSCRIPTION", transcription.id, self.minutes_config, "SUCCEEDED", 100, {"prompt_tokens": 10, "total_tokens": 10}, None),
            ("CALL-2", "TRANSCRIPTION", other_transcription.id, self.minutes_config, "FAILED", 150, None, "Other transcription failed"),
            ("CALL-3", "MINUTES", minutes.id, self.minutes_config, "SUCCEEDED", 200, {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}, None),
            ("CALL-5", "AGENT_REVIEW", self.run.id, self.agent_config, "FAILED", 400, None, "Agent model failed"),
            ("CALL-7", "UNKNOWN_CALL", self.employee.id, self.minutes_config, "SUCCEEDED", 30, {"total_tokens": 100}, None),
        ]
        for request_id, call_type, biz_id, config, status, elapsed, usage, error in log_rows:
            await AiCallLog.create(
                request_id=request_id, call_type=call_type, biz_id=biz_id, model_config_id=config.id,
                model_name=config.model_name, status=status, elapsed_ms=elapsed, usage_json=usage, error_message=error,
            )

        await AgentStep.create(
            run_id=self.run.id, step_no=1, round_no=1, step_type="REVIEW", status="SUCCEEDED", elapsed_ms=120,
        )
        await AgentStep.create(
            run_id=self.run.id, step_no=2, round_no=1, step_type="REFINE", status="FAILED", elapsed_ms=200,
            error_message="Refine failed",
        )
        await AgentStep.create(
            run_id=self.run.id, step_no=3, round_no=2, step_type="REVIEW", status="SUCCEEDED", elapsed_ms=90,
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def test_token_usage_supports_responses_and_embedding_shapes(self):
        self.assertEqual(token_usage({"input_tokens": 8, "output_tokens": 2}), (8, 2, 10))
        self.assertEqual(token_usage({"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10}), (6, 4, 10))
        self.assertEqual(token_usage(None), (0, 0, 0))

    async def test_employee_observability_only_contains_owned_business_logs(self):
        # sqlite 存时间带时区偏移，测试统一用覆盖前后一天的范围，避开跨零点的边界
        start, end = date_range()
        result = await build_observability(self.employee, start, end, None)
        summary = result["summary"]
        self.assertEqual(result["scope"], "个人AI与Agent观测")
        self.assertEqual(summary["call_count"], 3)
        self.assertEqual(summary["failed_call_count"], 1)
        self.assertEqual(summary["call_success_rate"], 66.67)
        self.assertEqual(summary["total_tokens"], 35)
        self.assertEqual(summary["agent_run_count"], 1)
        self.assertEqual(summary["agent_success_rate"], 100.0)
        self.assertEqual(summary["step_count"], 3)
        self.assertEqual(summary["step_failure_rate"], 33.33)
        self.assertEqual(summary["revision_count"], 1)
        self.assertEqual(summary["average_rounds"], 2.0)
        self.assertEqual(result["recent_failures"][0]["error_message"], "Agent model failed")
        self.assertNotIn("UNKNOWN_CALL", {item["call_type"] for item in result["call_types"]})

    async def test_admin_sees_global_logs_and_model_filter(self):
        # sqlite 存时间带时区偏移，测试统一用覆盖前后一天的范围，避开跨零点的边界
        start, end = date_range()
        global_result = await build_observability(self.admin, start, end, None)
        self.assertEqual(global_result["summary"]["call_count"], 5)
        self.assertEqual(global_result["summary"]["agent_run_count"], 2)
        filtered = await build_observability(self.admin, start, end, self.agent_config.id)
        self.assertEqual(filtered["summary"]["call_count"], 1)
        self.assertEqual(filtered["summary"]["agent_run_count"], 2)
        self.assertEqual(filtered["summary"]["step_count"], 3)

    async def test_invalid_model_filter_is_rejected(self):
        # sqlite 存时间带时区偏移，测试统一用覆盖前后一天的范围，避开跨零点的边界
        start, end = date_range()
        with self.assertRaises(CustomException):
            await build_observability(self.employee, start, end, 999999)


class EmployeeCanSeeLogTest(unittest.TestCase):
    """员工视角的调用归属：重构后新增的调用类型不能再被静默过滤。"""

    @staticmethod
    def _log(call_type: str, biz_id: int):
        from types import SimpleNamespace
        return SimpleNamespace(call_type=call_type, biz_id=biz_id)

    def setUp(self):
        self.owned = {"TRANSCRIPTION": {10}, "MINUTES": {20}, "AGENT": {30}}

    def test_all_minutes_call_types_map_to_minutes_ownership(self):
        from services.observability_service import employee_can_see_log
        for call_type in ("MINUTES",):
            self.assertTrue(employee_can_see_log(self._log(call_type, 20), self.owned), call_type)
            self.assertFalse(employee_can_see_log(self._log(call_type, 999), self.owned), call_type)

    def test_agent_review_support_maps_to_agent_ownership(self):
        from services.observability_service import employee_can_see_log
        for call_type in ("AGENT_REVIEW", "AGENT_REFINE", "AGENT_REVIEW_SUPPORT"):
            self.assertTrue(employee_can_see_log(self._log(call_type, 30), self.owned), call_type)
            self.assertFalse(employee_can_see_log(self._log(call_type, 20), self.owned), call_type)

    def test_unknown_call_type_still_hidden(self):
        from services.observability_service import employee_can_see_log
        self.assertFalse(employee_can_see_log(self._log("SPEAKER_MATCH", 20), self.owned))
        self.assertFalse(employee_can_see_log(self._log("SOMETHING_NEW", 30), self.owned))


if __name__ == "__main__":
    unittest.main()
