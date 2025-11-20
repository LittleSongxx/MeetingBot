import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tortoise import Tortoise

from api.agent import ApplyPayload, RunPayload, apply, interrupt, resume
from api.prompt_template import PromptPayload, validate_prompt
from common.exception_handler import CustomException
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
from common.model_json import parse_model_json
from services import contracts
from services.agent_service import (
    MAX_TRANSCRIPT_CHARS,
    execute_agent_run,
    get_transcript_char_limit,
    issue_line,
    transcript_excerpt,
)


class AgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
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
            system_prompt="writer", user_prompt="{meeting_title}\n原文：{transcript}\n纪要：{minutes_json}\n审核：{issues}", enabled=True,
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
        # 超了才截断，并准确说明提供的字符范围。
        await TranscriptionTask.filter(id=self.task.id).update(full_text="话" * (MAX_TRANSCRIPT_CHARS + 100))
        excerpt = await transcript_excerpt(self.task.id)
        self.assertTrue(excerpt.startswith("话" * 10))
        self.assertIn(f"仅提供前{MAX_TRANSCRIPT_CHARS}个字符", excerpt)
        self.assertIn("后续内容未提供", excerpt)

    def test_review_context_limit_is_configurable_and_positive(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_transcript_char_limit(), 100000)
        with patch.dict("os.environ", {"AGENT_REVIEW_MAX_TRANSCRIPT_CHARS": "45000"}):
            self.assertEqual(get_transcript_char_limit(), 45000)
        for invalid in ("0", "-1", "not-a-number", ""):
            with self.subTest(value=invalid), patch.dict(
                "os.environ", {"AGENT_REVIEW_MAX_TRANSCRIPT_CHARS": invalid}
            ):
                with self.assertRaisesRegex(ValueError, "AGENT_REVIEW_MAX_TRANSCRIPT_CHARS"):
                    get_transcript_char_limit()

    async def test_review_prompt_preserves_evidence_after_twenty_thousand_chars(self):
        """32728字符会议的尾部事实实际进入审查调用，避免只测截取函数。"""
        evidence = "尾部决策：归档版本由周岚复核，原始录音保留三十天。"
        transcript = "会议讨论。" * 6000
        transcript += "补" * (32728 - len(transcript) - len(evidence)) + evidence
        self.assertEqual(len(transcript), 32728)
        self.assertGreater(transcript.index(evidence), 20000)
        await TranscriptionTask.filter(id=self.task.id).update(full_text=transcript)
        await MeetingMinutes.filter(id=self.minutes.id).update(summary=evidence)
        run = await self.create_run()
        model = AsyncMock(return_value={"passed": True, "score": 95, "conclusion": "有原文依据", "issues": []})
        with patch.dict("os.environ", {}, clear=True):
            default_limit = get_transcript_char_limit()
        with patch("services.agent_service.MAX_TRANSCRIPT_CHARS", default_limit), patch(
            "services.agent_service.call_model", new=model
        ):
            await execute_agent_run(run.id)
        model.assert_awaited_once()
        self.assertEqual(model.await_args.args[5], "AGENT_REVIEW")
        self.assertIn(transcript, model.await_args.args[3])
        self.assertNotIn("后续内容未提供", model.await_args.args[3])
        self.assertEqual((await AgentRun.get(id=run.id)).status, "SUCCEEDED")

    async def test_review_context_truncates_only_above_configured_boundary(self):
        with patch("services.agent_service.MAX_TRANSCRIPT_CHARS", 10):
            await TranscriptionTask.filter(id=self.task.id).update(full_text="甲" * 10)
            self.assertEqual(await transcript_excerpt(self.task.id), "甲" * 10)
            await TranscriptionTask.filter(id=self.task.id).update(full_text="甲" * 10 + "尾部证据")
            excerpt = await transcript_excerpt(self.task.id)
        self.assertTrue(excerpt.startswith("甲" * 10 + "\n"))
        self.assertNotIn("尾部证据", excerpt)
        self.assertIn("转写原文共14个字符", excerpt)
        self.assertIn("仅提供前10个字符", excerpt)
        self.assertIn("不能据此断言后文不存在相关依据", excerpt)

    async def test_refine_receives_same_bounded_transcript_as_review(self):
        """审查和修订都看到同一份节选，超限尾部不能被当作已提供的证据。"""
        transcript = "已提供的事实：张涛负责。" + "甲" * 40 + "未提供的尾部证据"
        await TranscriptionTask.filter(id=self.task.id).update(full_text=transcript)
        run = await self.create_run(max_rounds=1)
        model = AsyncMock(side_effect=[self.failing_review(), self.support_ok(), self.refined()])
        with patch("services.agent_service.MAX_TRANSCRIPT_CHARS", 30), patch(
            "services.agent_service.call_model", new=model
        ):
            excerpt = await transcript_excerpt(self.task.id)
            await execute_agent_run(run.id)
        self.assertEqual([call.args[5] for call in model.await_args_list],
                         ["AGENT_REVIEW", "AGENT_REVIEW_SUPPORT", "AGENT_REFINE"])
        for call in model.await_args_list:
            self.assertIn(excerpt, call.args[3])
            self.assertNotIn("未提供的尾部证据", call.args[3])
        self.assertEqual((await AgentRun.get(id=run.id)).status, "WAITING_CONFIRMATION")

    async def test_refine_template_edit_requires_transcript_but_existing_template_still_runs(self):
        template = await PromptTemplate.get(code="MINUTES_REFINE")
        old_user_prompt = "{meeting_title}{minutes_json}{issues}"
        payload = PromptPayload(
            id=template.id, code=template.code, name=template.name,
            system_prompt=template.system_prompt, user_prompt=old_user_prompt,
        )
        with self.assertRaises(CustomException) as context:
            await validate_prompt(payload)
        self.assertIn("{transcript}", context.exception.message)
        payload.user_prompt += "{transcript}"
        await validate_prompt(payload)

        # 旧数据库模板尚未被管理员更新时，运行时传入额外变量不会中断自检。
        await PromptTemplate.filter(id=template.id).update(user_prompt=old_user_prompt)
        run = await self.create_run(max_rounds=1)
        model = AsyncMock(side_effect=[self.failing_review(), self.support_ok(), self.refined()])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        self.assertEqual((await AgentRun.get(id=run.id)).status, "WAITING_CONFIRMATION")
        self.assertEqual(model.await_args_list[2].args[5], "AGENT_REFINE")
        # 中间那次是支撑复核：审查意见要先过"依据是否支撑该判定"
        self.assertEqual(model.await_args_list[1].args[5], "AGENT_REVIEW_SUPPORT")

    def test_model_json_and_issue_line(self):
        # 模型把 JSON 包在代码块里时，解析器剥掉围栏；返回数组或非 JSON 文本时抛异常
        self.assertEqual(parse_model_json('```json\n{"passed": true}\n```'), {"passed": True})
        with self.assertRaises(Exception):
            parse_model_json("[1, 2]")
        with self.assertRaises(Exception):
            parse_model_json("这不是 JSON")
        line = issue_line({"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER", "detail": "没写负责人", "suggestion": "补上张涛"})
        self.assertIn("决策第1条", line)
        self.assertIn("没有明确负责人", line)

    async def test_reflection_loop_stops_when_model_passes(self):
        """模型第二轮判定通过，反思环就停在第二轮，而不是把 max_rounds 跑满。"""
        run = await self.create_run(max_rounds=3)
        responses = [
            # 意见要带**能在原文定位**的前提，否则会被证据门控挡下、这一轮直接判通过，
            # 反思环就进不到第二轮（本测试要测的正是第二轮的停止行为）
            {"passed": False, "score": 60, "conclusion": "决策没有负责人", "issues": [
                {"field": "decisions", "index": 0, "issue_type": "MISSING_OWNER",
                 "detail": "没写负责人", "suggestion": "补上张涛",
                 "source_span": "张涛负责", "target_text": "张涛负责"}
            ]},
            self.support_ok(),
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

    async def test_conflicting_review_fails_before_persisting_a_verdict(self):
        """模型矛盾判定不得跳过修订，也不得留下成功的审查步骤。"""
        for passed, issues in ((True, [{
            "field": "decisions", "index": 0, "issue_type": "MISSING_OWNER",
            "detail": "缺少负责人", "suggestion": "补充负责人",
            "source_span": "张涛负责", "target_text": "张涛负责",
        }]), (False, [])):
            with self.subTest(passed=passed, issues=issues):
                run = await self.create_run()
                model = AsyncMock(return_value={
                    "passed": passed, "score": 80, "issues": issues, "conclusion": "矛盾结果",
                })
                with patch("services.agent_service.call_model", new=model):
                    await execute_agent_run(run.id)
                run = await AgentRun.get(id=run.id)
                steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
                self.assertEqual(run.status, "FAILED")
                self.assertIn("审查结果矛盾", run.error_message)
                self.assertEqual(run.current_round, 0)
                self.assertIsNone(run.revised_json)
                self.assertEqual([(step.step_type, step.status) for step in steps], [("REVIEW", "FAILED")])
                self.assertEqual(model.await_count, 1)  # 无意见 → 不触发支撑复核
                self.assertEqual((await MeetingMinutes.get(id=self.minutes.id)).summary, "讨论了报表导出")

    @staticmethod
    def support_ok(count=1):
        """支撑复核的返回：全部判为"依据支撑该意见"（本测试只关心后续流程）。"""
        return {"verdicts": [{"index": i, "supported": True, "why": "依据支撑该判定"}
                             for i in range(1, count + 1)]}

    def failing_review(self):
        # 意见必须带证据才过门控（真实模型 100% 会给 source_span/target_text）：
        # 缺失类给"原文里确实有"的前提，否则会被证据门控挡下、这一轮直接判通过。
        return {"passed": False, "score": 50, "conclusion": "仍有问题", "issues": [
            {"field": "decisions", "index": 0, "issue_type": "MISSING_DEADLINE",
             "detail": "没写时间", "suggestion": "补上日期",
             "source_span": "张涛负责", "target_text": "张涛负责"}
        ]}

    async def test_single_round_is_review_then_refine(self):
        """默认的一轮就是「审查一次 + 按问题改一版」，改完不再复审。"""
        run = await self.create_run(max_rounds=1)
        model = AsyncMock(side_effect=[self.failing_review(), self.support_ok(), self.refined()])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "REFINE", "FINAL"])
        self.assertEqual(run.current_round, 1)
        self.assertFalse(run.passed)
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        # 存储形态已是契约 v2 的类型化槽位；展示用字符串由 present_output 产出
        self.assertEqual(run.revised_json["decisions"][0]["owner"]["text"], "张涛")
        # 只调了审查和重写两次，没有第三次复审
        self.assertEqual(model.await_count, 3)  # 审查 + 支撑复核 + 重写

    async def test_round_limit_stops_the_loop(self):
        """模型一直判不通过时，代码兜住上限，不会无限转下去。"""
        run = await self.create_run(max_rounds=2)
        model = AsyncMock(side_effect=[self.failing_review(), self.support_ok(), self.refined(),
                          self.failing_review(), self.support_ok(), self.refined()])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "REFINE", "REVIEW", "REFINE", "FINAL"])
        self.assertEqual(run.current_round, 2)
        self.assertFalse(run.passed)
        self.assertEqual(run.issue_count, 1)
        self.assertEqual(run.status, "WAITING_CONFIRMATION")
        self.assertEqual(model.await_count, 6)  # 两轮 × (审查 + 支撑复核 + 重写)

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
        # 写回时会经 upgrade_output 收敛到当前契约，因此存储里是类型化槽位
        self.assertEqual(minutes.decisions_json[0]["owner"]["text"], "张涛")
        self.assertEqual(
            contracts.present_minutes_fields(None, decisions=minutes.decisions_json)["decisions"][0][
                "owner_suggestion"
            ],
            "张涛",
        )

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


    async def test_issue_failing_the_support_check_is_dropped_and_the_round_passes(self):
        """字面定位通过 ≠ 依据支撑该判定：第二层挡下后，本轮应判通过而不是硬改一版。

        实测依据：独立模型族在业界口径下只认可 25% 的 `UNSUPPORTED` 意见，
        而按字面定位它们全都"有依据"——所以定位之后必须再过一层支撑判定。
        """
        run = await self.create_run(max_rounds=1)
        model = AsyncMock(side_effect=[
            self.failing_review(),
            # 复核结论：依据不支撑该判定
            {"verdicts": [{"index": 1, "supported": False, "why": "依据只沾到边缘"}]},
        ])
        with patch("services.agent_service.call_model", new=model):
            await execute_agent_run(run.id)
        run = await AgentRun.get(id=run.id)
        steps = await AgentStep.filter(run_id=run.id).order_by("step_no")
        # 没有可靠的修改依据 → 不进重写步，本轮通过
        self.assertEqual([step.step_type for step in steps], ["REVIEW", "FINAL"])
        self.assertTrue(run.passed)
        self.assertEqual(run.status, "SUCCEEDED")
        review_step = steps[0]
        gate = (review_step.payload_json or {}).get("evidence_gate") or {}
        self.assertEqual(gate.get("dropped_by_support"), 1)
        self.assertEqual(gate.get("final_kept"), 0)
        # 被挡下的意见原样留存，供人查看
        dropped = (review_step.payload_json or {}).get("issues_dropped_by_support_check") or []
        self.assertEqual(len(dropped), 1)
        self.assertIn("premise_does_not_support_issue", dropped[0]["reasons"])


if __name__ == "__main__":
    unittest.main()


class AcceptanceInstrumentationTest(unittest.TestCase):
    """审阅埋点：presented/submitted 快照 + ui_mode + 逐条决定，写进 final_result。"""

    def _payload(self, **overrides):
        from api.agent import ApplyItemDecision, ApplyPayload
        base = {"approved": True, "ui_mode": "change_list",
                "item_decisions": [ApplyItemDecision(field="topics", key="试点",
                                                     action="accepted")]}
        base.update(overrides)
        return ApplyPayload(**base)

    def _run(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            final_result={"conclusion": "自检完成"},
            revised_json={"summary": "s", "topics": [{"title": "试点"}],
                          "viewpoints": [], "decisions": [], "pending_items": [], "risks": []},
        )

    def test_record_snapshots_presented_and_submitted_and_keeps_other_keys(self):
        from api.agent import _acceptance_record
        run = self._run()
        record = _acceptance_record(run, self._payload(), submitted_revision=run.revised_json)
        self.assertEqual(record["conclusion"], "自检完成")  # 不整体覆盖已有键
        acceptance = record["acceptance"]
        self.assertEqual(acceptance["decision"], "approved")
        self.assertEqual(acceptance["ui_mode"], "change_list")
        self.assertEqual(acceptance["item_decisions"][0]["action"], "accepted")
        self.assertEqual(acceptance["presented"]["topics"], [{"title": "试点"}])
        self.assertEqual(acceptance["submitted"]["topics"], [{"title": "试点"}])
        self.assertIn("decided_at", acceptance)

    def test_rejection_has_no_submitted_snapshot(self):
        from api.agent import _acceptance_record
        run = self._run()
        payload = self._payload(approved=False, item_decisions=[])
        acceptance = _acceptance_record(run, payload, submitted_revision=None)["acceptance"]
        self.assertEqual(acceptance["decision"], "rejected")
        self.assertIsNone(acceptance["submitted"])
        self.assertIsNotNone(acceptance["presented"])

    def test_non_dict_revision_snapshots_to_none_not_an_error(self):
        from api.agent import _acceptance_record
        run = self._run()
        run.revised_json = None
        acceptance = _acceptance_record(run, self._payload(), None)["acceptance"]
        self.assertIsNone(acceptance["presented"])


class FinalResultRoundTripTest(unittest.TestCase):
    """final_result 列是 TextField：结构化内容必须 dump 落库、load 读回，
    纯文本旧数据归一成 {"text": ...}，自检结论不被审阅埋点覆盖。"""

    def test_dump_load_round_trip(self):
        from services.agent_service import dump_final_result, load_final_result
        payload = {"text": "自检通过", "acceptance": {"decision": "approved"}}
        self.assertEqual(load_final_result(dump_final_result(payload)), payload)

    def test_plain_text_legacy_value_becomes_text_key(self):
        from services.agent_service import load_final_result
        self.assertEqual(load_final_result("自检通过，得分 90"), {"text": "自检通过，得分 90"})
        self.assertEqual(load_final_result(None), {})
        self.assertEqual(load_final_result(""), {})

    def test_acceptance_keeps_legacy_summary_under_text_key(self):
        from api.agent import _acceptance_record, ApplyPayload
        from types import SimpleNamespace
        run = SimpleNamespace(
            final_result="经过 2 轮自检，等待人工确认。",
            revised_json={"summary": "s", "topics": [], "viewpoints": [],
                          "decisions": [], "pending_items": [], "risks": []},
        )
        payload = ApplyPayload(approved=False, ui_mode="change_list", item_decisions=[])
        record = _acceptance_record(run, payload, None)
        self.assertEqual(record["text"], "经过 2 轮自检，等待人工确认。")
        self.assertEqual(record["acceptance"]["decision"], "rejected")
