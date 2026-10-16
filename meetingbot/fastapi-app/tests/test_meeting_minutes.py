import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, patch

from api.meeting_minutes import (
    EditPayload,
    build_docx,
    build_pdf,
    confirm,
    force_fail,
    generate,
    get_accessible_minutes,
    minutes_dict,
    select_page,
    unconfirm,
    update,
)
from common.exception_handler import CustomException
from models import (
    AiModelConfig,
    Meeting,
    MeetingMinutes,
    MeetingParticipant,
    PromptTemplate,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from pydantic import ValidationError
from services import contracts, minutes_service
from services.minutes_service import MinutesResult, execute_minutes
from tortoise import Tortoise


class MeetingMinutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.creator = await User.create(username="minutes_creator", password="123456", name="创建人", role="EMPLOYEE")
        self.participant = await User.create(username="minutes_member", password="123456", name="参会人", role="EMPLOYEE")
        self.outsider = await User.create(username="minutes_outsider", password="123456", name="无关员工", role="EMPLOYEE")
        self.meeting = await Meeting.create(
            meeting_no="MTG-MINUTES-001", title="产品评审会", start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00", creator_id=self.creator.id, host_id=self.creator.id,
        )
        await MeetingParticipant.create(meeting_id=self.meeting.id, user_id=self.participant.id)
        self.config = await AiModelConfig.create(
            name="纪要模型", model_type="MINUTES", base_url="https://api.openai.com/v1",
            api_key="unit-test-key", model_name="gpt-4.1-mini", enabled=True,
        )
        self.task = await TranscriptionTask.create(
            meeting_id=self.meeting.id, material_id=1, model_config_id=self.config.id,
            initiator_id=self.creator.id, status="SUCCEEDED", full_text="确认发布计划。",
        )
        await TranscriptSegment.create(
            task_id=self.task.id, meeting_id=self.meeting.id, segment_no=1, start_ms=0, end_ms=5000,
            speaker_label="A", speaker_user_id=self.creator.id, content="确认九月一日发布，由张三负责。", original_text="确认九月一日发布，由张三负责。",
        )
        await PromptTemplate.create(
            code="MEETING_MINUTES", name="纪要生成", system_prompt="只能根据原文总结",
            user_prompt="会议：{meeting_title}\n转写：{transcript}", enabled=True,
        )
        self.ai_result = MinutesResult.model_validate({
            "summary": "会议确认产品发布计划。",
            "topics": [{"title": "发布计划", "summary": "九月一日发布"}],
            "viewpoints": [{"speaker": "A", "viewpoint": "按期发布"}],
            "decisions": [{"content": "九月一日发布", "basis": "评审通过", "owner_suggestion": "张三", "deadline_suggestion": "2026-09-01"}],
            "pending_items": [],
            "risks": [{"content": "测试时间紧张", "level": "MEDIUM", "suggestion": "增加回归测试"}],
        })

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def create_draft(self):
        with patch("api.meeting_minutes.schedule_minutes") as schedule:
            result = await generate(self.task.id, self.participant)
            schedule.assert_called_once_with(result.data["id"])
        with patch("services.minutes_service.call_minutes_model", new=AsyncMock(return_value=self.ai_result)):
            await execute_minutes(result.data["id"])
        return await MeetingMinutes.get(id=result.data["id"])

    async def test_generate_edit_confirm_and_permissions(self):
        minutes = await self.create_draft()
        self.assertEqual(minutes.status, "DRAFT")
        self.assertEqual(minutes.progress, 100)
        # 契约 v2：存储是类型化槽位，展示字符串走表现适配
        self.assertEqual(minutes.decisions_json[0]["owner"]["text"], "张三")
        self.assertEqual(
            contracts.present_minutes_fields(None, decisions=minutes.decisions_json)["decisions"][0][
                "owner_suggestion"
            ],
            "张三",
        )

        payload = EditPayload(id=minutes.id, **self.ai_result.model_dump())
        payload.summary = "人工修订后的会议摘要。"
        await update(payload, self.participant)
        minutes = await MeetingMinutes.get(id=minutes.id)
        self.assertEqual(minutes.summary, "人工修订后的会议摘要。")

        await confirm(minutes.id, self.participant)
        minutes = await MeetingMinutes.get(id=minutes.id)
        self.assertEqual(minutes.status, "CONFIRMED")
        self.assertEqual(minutes.confirmed_by, self.participant.id)
        with self.assertRaises(CustomException):
            await update(payload, self.participant)
        with self.assertRaises(CustomException) as context:
            await get_accessible_minutes(minutes.id, self.outsider)
        self.assertEqual(context.exception.code, "403")

    async def test_unconfirm_returns_minutes_to_draft(self):
        """取消确认把纪要退回草稿，修订入口重新打开。"""
        minutes = await self.create_draft()
        await confirm(minutes.id, self.participant)
        # 纪要是 participant 发起生成的，creator 虽然能看这场会议，但不是纪要创建人
        with self.assertRaises(CustomException) as context:
            await unconfirm(minutes.id, self.creator)
        self.assertEqual(context.exception.code, "403")
        await unconfirm(minutes.id, self.participant)
        minutes = await MeetingMinutes.get(id=minutes.id)
        self.assertEqual(minutes.status, "DRAFT")
        self.assertIsNone(minutes.confirmed_by)
        self.assertIsNone(minutes.confirmed_time)
        # 退回草稿之后又能改内容了
        payload = EditPayload(id=minutes.id, **self.ai_result.model_dump())
        payload.summary = "退回草稿后再改一次。"
        await update(payload, self.participant)
        self.assertEqual((await MeetingMinutes.get(id=minutes.id)).summary, "退回草稿后再改一次。")

    async def test_employee_page_only_returns_accessible_meetings(self):
        minutes = await self.create_draft()
        participant_result = await select_page(current_user=self.participant)
        outsider_result = await select_page(current_user=self.outsider)
        self.assertEqual(participant_result.data["total"], 1)
        self.assertEqual(participant_result.data["list"][0]["id"], minutes.id)
        self.assertEqual(outsider_result.data["total"], 0)

    async def test_word_pdf_export(self):
        minutes = await self.create_draft()
        data = await minutes_dict(minutes)
        self.assertEqual(build_docx(data).read(2), b"PK")
        self.assertEqual(build_pdf(data).read(4), b"%PDF")

    async def test_single_pass_full_transcript_with_matched_speaker_name(self):
        """单遍长上下文：整场转写一次调用，说话人行使用匹配好的真实姓名。"""
        await TranscriptSegment.create(
            task_id=self.task.id, meeting_id=self.meeting.id, segment_no=2, start_ms=5000, end_ms=9000,
            speaker_label="B", content="补充执行风险。", original_text="补充执行风险。",
        )
        minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id, source_task_id=self.task.id, model_config_id=self.config.id,
            status="GENERATING", created_by=self.creator.id,
        )
        model_call = AsyncMock(return_value=self.ai_result)
        with patch("services.minutes_service.call_minutes_model", new=model_call):
            await execute_minutes(minutes.id)
        minutes = await MeetingMinutes.get(id=minutes.id)
        self.assertEqual(minutes.status, "DRAFT")
        self.assertEqual(minutes.stage, "纪要已生成")
        model_call.assert_awaited_once()
        # 整场转写都在同一次调用里，且说话人行用真实姓名（创建人）而非标签 A
        prompt_text = model_call.await_args_list[0].args[3]
        self.assertIn("确认九月一日发布，由张三负责。", prompt_text)
        self.assertIn("[创建人]", prompt_text)
        self.assertIn("补充执行风险。", prompt_text)

    async def test_force_fail_requests_workflow_cancellation(self):
        """强制结束不仅要置 FAILED，还要请求取消 workflow：活着的协程会把状态
        写回 GENERATING，且重试的同 id 入队会被拒——对齐转写 forceFail 的语义。"""
        with patch("api.meeting_minutes.schedule_minutes"):
            result = await generate(self.task.id, self.participant)
        minutes_id = result.data["id"]
        with self.assertRaises(CustomException) as context:
            await force_fail(minutes_id, self.outsider)
        self.assertEqual(context.exception.code, "403")
        with patch("api.meeting_minutes.cancel_minutes") as cancel:
            await force_fail(minutes_id, self.participant)
            cancel.assert_called_once_with(minutes_id)
        minutes = await MeetingMinutes.get(id=minutes_id)
        self.assertEqual(minutes.status, "FAILED")
        self.assertEqual(minutes.stage, "已被强制结束")

    def test_risk_level_is_structurally_validated(self):
        invalid = self.ai_result.model_dump()
        invalid["risks"][0]["level"] = "UNKNOWN"
        with self.assertRaises(ValidationError):
            MinutesResult.model_validate(invalid)


class MinutesCallCacheTest(unittest.TestCase):
    """缓存载荷必须自带"模型当时给过哪些字段"的凭据（model_fields_set）。

    model_dump() 会把默认值补成全字段，is_unprovided_content_generation 读的
    区别恰恰在这份信息里——不随缓存存下来，读侧就无法重放"五个列表字段
    当时就没给"的护栏，只能信任写入方的约定。
    """

    def test_roundtrip_preserves_the_provided_fields_evidence(self):
        complete = MinutesResult.model_validate({
            "summary": "s",
            "decisions": [{"content": "d", "basis": "b"}],
        })
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(minutes_service, "MINUTES_CACHE_DIR", Path(tmp)),
              patch.object(minutes_service, "_CACHE_ENABLED", True)):
            minutes_service._store_call_cache("model", "sys", "user", complete)
            payload = minutes_service._load_call_cache("model", "sys", "user")
            self.assertIsNotNone(payload)
            self.assertFalse(minutes_service._cache_unprovided_content(payload))
            self.assertEqual(
                MinutesResult.model_validate(payload["result"]).decisions[0].content, "d")
            # 键不同（模型或提示词变了）读不到旧结果
            self.assertIsNone(minutes_service._load_call_cache("other", "sys", "user"))

    def test_payload_without_evidence_is_not_trusted(self):
        # 旧格式（裸 model_dump，无 provided 键）与全缺席载荷一律不可信
        self.assertTrue(minutes_service._cache_unprovided_content({"summary": "s"}))
        self.assertTrue(minutes_service._cache_unprovided_content(
            {"provided": ["summary"], "result": {"summary": "s"}}))
        # 只要不是"五个列表字段全部缺席"就可信（个别缺席是合法形态）
        self.assertFalse(minutes_service._cache_unprovided_content(
            {"provided": ["summary", "topics"], "result": {}}))


if __name__ == "__main__":
    unittest.main()
