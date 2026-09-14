import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tortoise import Tortoise

from api.agent import ApplyPayload, RunPayload, apply, interrupt, resume
from common.exception_handler import CustomException
from common.model_json import parse_model_json
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
from services.agent_service import (
    MAX_TRANSCRIPT_CHARS,
    execute_agent_run,
    issue_line,
    transcript_excerpt,
)


class AgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.owner = await User.create(username="agent_owner", password="123456", name="Agent Owner", role="EMPLOYEE")
        self.admin = await User.create(username="agent_admin", password="123456", name="Administrator", role="ADMIN")
        self.outsider = await User.create(username="agent_outsider", password="123456", name="Outsider", role="EMPLOYEE")
        self.meeting = await Meeting.create(
            meeting_no="MTG-AGENT-001",
            title="Product release review",
            start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00",
            creator_id=self.owner.id,
            host_id=self.owner.id,
            status="FINISHED",
        )
        await MeetingParticipant.create(meeting_id=self.meeting.id, user_id=self.owner.id)
        self.config = await AiModelConfig.create(
            name="Review model",
            model_type="AGENT",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="key",
            model_name="qwen-plus",
            enabled=True,
        )
        await PromptTemplate.create(
            code="MINUTES_REVIEW", name="review", scene_type="AGENT",
            system_prompt="reviewer", user_prompt="{meeting_title}{minutes_json}{transcript}", enabled=True,
        )
        await PromptTemplate.create(
            code="MINUTES_REFINE", name="refine", scene_type="AGENT",
            system_prompt="writer", user_prompt="{meeting_title}{minutes_json}{issues}", enabled=True,
        )
        self.task = await TranscriptionTask.create(
            task_no="TSK-AGENT-001", meeting_id=self.meeting.id, material_id=1,
            model_config_id=self.config.id, initiator_id=self.owner.id, status="SUCCEEDED",
            full_text="王敏说报表导出要改成异步，张涛负责。",
        )
        self.minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=self.task.id,
            model_config_id=self.config.id, created_by=self.owner.id, status="DRAFT",
            summary="讨论了报表导出", decisions_json=[{"content": "报表导出改异步", "basis": "王敏提出", "owner_suggestion": "", "deadline_suggestion": ""}],
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def create_run(self, status="RUNNING", max_rounds=3):
        return await AgentRun.create(
            run_no="AGR-" + str(await AgentRun.all().count() + 1),
            minutes_id=self.minutes.id,
            meeting_id=self.meeting.id,
            user_id=self.owner.id,
            model_config_id=self.config.id,
            status=status,
            max_rounds=max_rounds,
        )

    def refined(self, owner="张涛"):
        return {
            "summary": "讨论了报表导出改造",
            "topics": [],
            "viewpoints": [],
            "decisions": [{"content": "报表导出改异步", "basis": "王敏提出", "owner_suggestion": owner, "deadline_suggestion": "2026-09-05"}],
            "pending_items": [],
            "risks": [],
        }

    def test_default_round_limit_is_two(self):
        """页面不传轮数时默认两轮：第一轮改完的稿子一定会被第二轮的审查复核一遍。"""
        self.assertEqual(RunPayload(minutes_id=1).max_rounds, 2)

    async def test_transcript_excerpt_covers_one_hour_meeting(self):
        """一小时的会议按每分钟 240 字算是 14400 字，要能完整喂给审查步。"""
        one_hour_chars = 60 * 240
        self.assertGreater(MAX_TRANSCRIPT_CHARS, one_hour_chars)
        # 没超上限时原样返回，模型看到的就是完整转写
        await TranscriptionTask.filter(id=self.task.id).update(full_text="话" * one_hour_chars)
        self.assertEqual(len(await transcript_excerpt(self.task.id)), one_hour_chars)
        # 超了才截断，并在末尾说明只给了前半部分
        await TranscriptionTask.filter(id=self.task.id).update(full_text="话" * (MAX_TRANSCRIPT_CHARS + 100))
        excerpt = await transcript_excerpt(self.task.id)
        self.assertTrue(excerpt.startswith("话" * 10))
        self.assertIn("只截取前半部分", excerpt)

    def test_model_json_and_issue_line(self):
        # 模型把 JSON 包在代码块里时，解析器剥掉围栏；返回数组或非 JSON 文本时抛异常
        self.assertEqual(parse_model_json('```json\n{"passed": true}\n```'), {"passed": True})
        with self.assertRaises(Exception):  # noqa: B017
            parse_model_json("[1, 2]")
        with self.assertRaises(Exception):  # noqa: B017
            parse_model_json("这不是 JSON")
        line = issue_line({"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER", "detail": "没写负责人", "suggestion": "补上张涛"})
        self.assertIn("决策第1条", line)
        self.assertIn("没有明确负责人", line)

    async def test_reflection_loop_stops_when_model_passes(self):
        """模型第二轮判定通过，反思环就停在第二轮，而不是把 max_rounds 跑满。"""
        run = await self.create_run(max_rounds=3)
        responses = [
            {"passed": False, "score": 60, "conclusion": "决策没有负责人", "issues": [
                {"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER", "detail": "没写负责人", "suggestion": "补上张涛"}
            ]},
            self.refined(),
            {"passed": True, "score": 92, "conclusion": "决策已可落实", "issues": []},
        ]
        with patch("services.agent_service.call_model", new=AsyncMock(side_effect=responses)):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual(run.current_round, 2)
        self.assertTrue(run.passed)
        self.assertEqual(run.issue_count, 0)
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "REFINE", "REVIEW", "FINAL"])
        # 改过一版，所以要等人工确认，纪要本身还没被动过
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        self.assertEqual((await MeetingMinutes.get(id=self.minutes.id)).summary, "讨论了报表导出")

    async def test_progress_moves_forward_and_ends_at_hundred(self):
        """反思环每开一步就把进度往前推，跑完置 100，页面的进度条靠它。"""
        run = await self.create_run(max_rounds=2)
        seen = []

        async def record(*args, **kwargs):
            # 每次调模型前记一下当前进度，用来确认它是一路往前走的
            seen.append((await AgentRun.get(id=run.id)).progress)
            return {"passed": True, "score": 90, "conclusion": "没问题", "issues": []}

        with patch("services.agent_service.call_model", new=record):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        # 第一次调模型时审查步已经开好，进度不再是 0
        self.assertGreater(seen[0], 0)
        self.assertEqual(run.progress, 100)
        self.assertEqual(run.stage, "自检通过")

    async def test_first_round_pass_finishes_without_confirmation(self):
        run = await self.create_run()
        with patch("services.agent_service.call_model", new=AsyncMock(return_value={"passed": True, "score": 95, "conclusion": "没有问题", "issues": []})):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(run.current_round, 1)
        self.assertIsNone(run.revised_json)

    def failing_review(self):
        return {"passed": False, "score": 50, "conclusion": "仍有问题", "issues": [
            {"field": "decisions", "index": 0, "issue_type": "MISSING_DEADLINE", "detail": "没写时间", "suggestion": "补上日期"}
        ]}

    async def test_single_round_is_review_then_refine(self):
        """默认的一轮就是「审查一次 + 按问题改一版」，改完不再复审。"""
        run = await self.create_run(max_rounds=1)
        model = AsyncMock(side_effect=[self.failing_review(), self.refined()])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "REFINE", "FINAL"])
        self.assertEqual(run.current_round, 1)
        self.assertFalse(run.passed)
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        self.assertEqual(run.revised_json["decisions"][0]["owner_suggestion"], "张涛")
        # 只调了审查和重写两次，没有第三次复审
        self.assertEqual(model.await_count, 2)

    async def test_round_limit_stops_the_loop(self):
        """模型一直判不通过时，代码兜住上限，不会无限转下去。"""
        run = await self.create_run(max_rounds=2)
        model = AsyncMock(side_effect=[self.failing_review(), self.refined(), self.failing_review(), self.refined()])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "REFINE", "REVIEW", "REFINE", "FINAL"])
        self.assertEqual(run.current_round, 2)
        self.assertFalse(run.passed)
        self.assertEqual(run.issue_count, 1)
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        self.assertEqual(model.await_count, 4)

    async def test_apply_writes_back_only_after_human_confirmation(self):
        run = await self.create_run(status="WAITING_CONFIRMATION")
        await AgentRun.filter(id=run.id).update(revised_json=self.refined(), passed=True, score=90, current_round=2)
        with self.assertRaises(CustomException) as context:
            await apply(run.id, ApplyPayload(approved=True), self.outsider)
        self.assertEqual(context.exception.code, "403")
        await apply(run.id, ApplyPayload(approved=True), self.owner)
        run = await AgentRun.get(id=run.id)
        minutes = await MeetingMinutes.get(id=self.minutes.id)
        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(run.applied_by, self.owner.id)
        self.assertEqual(minutes.summary, "讨论了报表导出改造")
        self.assertEqual(minutes.decisions_json[0]["owner_suggestion"], "张涛")

    async def test_reject_keeps_original_minutes(self):
        run = await self.create_run(status="WAITING_CONFIRMATION")
        await AgentRun.filter(id=run.id).update(revised_json=self.refined())
        await apply(run.id, ApplyPayload(approved=False, reason="与实际讨论不符"), self.admin)
        run = await AgentRun.get(id=run.id)
        self.assertEqual(run.status, "REJECTED")
        self.assertEqual(run.reject_reason, "与实际讨论不符")
        self.assertEqual((await MeetingMinutes.get(id=self.minutes.id)).summary, "讨论了报表导出")

    async def test_interrupt_resume_permission_and_state(self):
        run = await self.create_run()
        with self.assertRaises(CustomException) as context:
            await interrupt(run.id, self.outsider)
        self.assertEqual(context.exception.code, "403")
        with patch("api.agent.cancel_agent_run") as cancel:
            await interrupt(run.id, self.owner)
            cancel.assert_called_once_with(run.id)
        self.assertEqual((await AgentRun.get(id=run.id)).status, "INTERRUPTED")
        with patch("api.agent.schedule_agent_run") as schedule:
            await resume(run.id, self.owner)
            schedule.assert_called_once_with(run.id)
        self.assertEqual((await AgentRun.get(id=run.id)).status, "RUNNING")

    async def test_interrupted_run_does_not_call_model(self):
        run = await self.create_run(status="INTERRUPTED")
        model = AsyncMock()
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        model.assert_not_awaited()

    async def test_cancel_marks_unfinished_step_failed(self):
        """中断发生在模型调用中途时，那一步要记成失败，不能一直停在 RUNNING。"""
        run = await self.create_run()
        entered = asyncio.Event()

        async def slow_model(*args, **kwargs):
            # 模拟模型迟迟不返回，让取消恰好落在审查步的调用中途
            entered.set()
            await asyncio.sleep(3600)

        with patch("services.agent_service.call_model", new=slow_model):
            task = asyncio.create_task(execute_agent_run(run.id))
            await entered.wait()
            task.cancel()
            await task
        step = await AgentStep.get(run_id=run.id, step_no=1)
        self.assertEqual(step.status, "FAILED")
        self.assertIsNotNone(step.finish_time)

    async def test_resume_closes_orphan_running_step(self):
        """服务被强行关闭后续跑，上次停在 RUNNING 的步骤先记成失败，新步骤接着编号。"""
        run = await self.create_run(max_rounds=2)
        await AgentStep.create(run_id=run.id, step_no=1, round_no=1, step_type="REVIEW", status="RUNNING")
        passing = {"passed": True, "score": 90, "conclusion": "没问题", "issues": []}
        with patch("services.agent_service.call_model", new=AsyncMock(return_value=passing)):
            await execute_agent_run(run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual([(step.step_no, step.status) for step in steps], [(1, "FAILED"), (2, "SUCCEEDED"), (3, "SUCCEEDED")])


if __name__ == "__main__":
    unittest.main()
