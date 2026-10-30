import unittest
from unittest.mock import AsyncMock, patch

from models import (
    AgentRun,
    AgentStep,
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    MeetingParticipant,
    PromptTemplate,
    TranscriptionTask,
    User,
)
from services.agent_service import execute_agent_run, finalize_run
from services.minutes_service import execute_minutes
from tortoise import Tortoise


class RecoveryAndClaimTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.owner = await User.create(username="recovery_owner", password="123456", name="Owner", role="EMPLOYEE")
        self.meeting = await Meeting.create(
            meeting_no="MTG-RECOVERY-001", title="恢复验证",
            start_time="2026-09-01 09:00:00", end_time="2026-09-01 10:00:00",
            creator_id=self.owner.id, host_id=self.owner.id, status="FINISHED",
        )
        await MeetingParticipant.create(meeting_id=self.meeting.id, user_id=self.owner.id)
        self.config = await AiModelConfig.create(
            name="recovery-agent", model_type="AGENT",
            base_url="https://example.invalid/v1", api_key="key", model_name="qwen-plus", enabled=True,
        )
        await AiModelConfig.create(
            name="recovery-minutes", model_type="MINUTES",
            base_url="https://example.invalid/v1", api_key="key", model_name="qwen-plus", enabled=True,
        )
        await PromptTemplate.create(
            code="MINUTES_REVIEW", name="review", scene_type="AGENT",
            system_prompt="reviewer", user_prompt="{meeting_title}{minutes_json}{transcript}", enabled=True,
        )
        await PromptTemplate.create(
            code="MINUTES_REFINE", name="refine", scene_type="AGENT",
            system_prompt="writer", user_prompt="{meeting_title}{minutes_json}{issues}", enabled=True,
        )
        await PromptTemplate.create(
            code="MEETING_MINUTES_CHUNK", name="chunk", scene_type="MEETING_MINUTES",
            system_prompt="writer", user_prompt="{meeting_title}{transcript}", enabled=True,
        )
        self.task = await TranscriptionTask.create(
            task_no="TSK-RECOVERY-001", meeting_id=self.meeting.id, material_id=1,
            model_config_id=self.config.id, initiator_id=self.owner.id, status="SUCCEEDED",
            full_text="王敏说报表导出要改成异步，张涛负责。",
        )
        self.minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=self.task.id,
            model_config_id=self.config.id, created_by=self.owner.id, status="DRAFT",
            summary="讨论了报表导出",
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def create_run(self, status="RUNNING", current_round=0, max_rounds=2, review_json=None):
        return await AgentRun.create(
            run_no="AGR-RECOVERY-" + str(await AgentRun.all().count() + 1),
            minutes_id=self.minutes.id, meeting_id=self.meeting.id, user_id=self.owner.id,
            model_config_id=self.config.id, status=status, max_rounds=max_rounds,
            current_round=current_round, review_json=review_json or [],
        )

    def refined(self):
        return {
            "summary": "讨论了报表导出改造",
            "topics": [], "viewpoints": [],
            "decisions": [{"content": "报表导出改异步", "basis": "王敏提出",
                           "owner_suggestion": "张涛", "deadline_suggestion": "2026-09-05"}],
            "pending_items": [], "risks": [],
        }

    async def test_minutes_claim_skips_when_already_claimed(self):
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=self.task.id + 100,
            model_config_id=self.config.id, created_by=self.owner.id,
            status="GENERATING", stage="已领取",
        )
        model = AsyncMock()
        with patch("services.minutes_service.call_minutes_model", new=model):
            await execute_minutes(minutes.id)
        model.assert_not_awaited()
        self.assertEqual((await MeetingMinutes.get(id=minutes.id)).status, "GENERATING")

    async def test_agent_claim_skips_when_already_claimed(self):
        run = await self.create_run()
        await AgentRun.filter(id=run.id).update(stage="已领取")
        model = AsyncMock()
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        model.assert_not_awaited()

    async def test_resume_continues_refine_instead_of_reviewing_old_draft(self):
        """中断在重写步中途：恢复后应先按上一轮问题重写，而不是重审旧稿。"""
        issues = [{"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER",
                   "detail": "没写负责人", "suggestion": "补上张涛"}]
        run = await self.create_run(current_round=1, review_json=issues)
        await AgentStep.create(run_id=run.id, step_no=1, round_no=1, step_type="REVIEW", status="SUCCEEDED")
        await AgentStep.create(
            run_id=run.id, step_no=2, round_no=1, step_type="REFINE", status="FAILED",
            error_message="运行已中断，本步未完成",
        )
        responses = [self.refined(), {"passed": True, "score": 92, "conclusion": "已可落实", "issues": []}]
        model = AsyncMock(side_effect=responses)
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        # 旧 REVIEW/REFINE 保留，新增的第 3 步必须是 REFINE（续跑重写），第 4 步才是复审
        self.assertEqual(
            [(step.step_type, step.round_no) for step in steps],
            [("REVIEW", 1), ("REFINE", 1), ("REFINE", 1), ("REVIEW", 2), ("FINAL", 2)],
        )
        self.assertEqual(run.current_round, 2)
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        self.assertEqual(model.await_count, 2)

    async def test_resume_after_limit_finalizes_without_model_call(self):
        run = await self.create_run(current_round=2, max_rounds=2)
        model = AsyncMock()
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        model.assert_not_awaited()
        run = await AgentRun.get(id=run.id)
        self.assertEqual(run.status, "SUCCEEDED")
        self.assertIn("自检通过", run.stage)

    async def test_finalize_does_not_override_interrupted(self):
        run = await self.create_run(status="INTERRUPTED")
        await finalize_run(run.id)
        run = await AgentRun.get(id=run.id)
        self.assertEqual(run.status, "INTERRUPTED")
        self.assertEqual(await AgentStep.filter(run_id=run.id).count(), 0)


if __name__ == "__main__":
    unittest.main()
