import unittest
from unittest.mock import AsyncMock, patch

from api.meeting_minutes import AskPayload
from api.meeting_minutes import ask as ask_api
from common.exception_handler import CustomException
from models import AiModelConfig, Meeting, MeetingMinutes, PromptTemplate, TranscriptSegment, User
from services.minutes_qa_service import _validate_citations, ask, format_history
from tortoise import Tortoise


class MinutesQaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.owner = await User.create(username="qa_owner", password="123456", name="张晨", role="EMPLOYEE")
        self.outsider = await User.create(username="qa_outsider", password="123456", name="路人", role="EMPLOYEE")
        self.meeting = Meeting(
            meeting_no="MTG-QA-001",
            title="产品评审会",
            start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00",
            creator_id=self.owner.id,
            host_id=self.owner.id,
        )
        await self.meeting.save()
        self.minutes = await MeetingMinutes.create(
            meeting_id=self.meeting.id,
            source_task_id=1,
            model_config_id=1,
            status="DRAFT",
            summary="会议决定报表导出改为异步导出。",
            created_by=self.owner.id,
            pending_items_json=[
                {"content": "前端任务列表联调", "owner_suggestion": "李悦", "deadline_suggestion": "2026-09-10"},
            ],
        )
        await TranscriptSegment.create(
            task_id=1,
            meeting_id=self.meeting.id,
            segment_no=1,
            start_ms=0,
            end_ms=5000,
            speaker_label="A",
            speaker_user_id=self.owner.id,
            original_text="报表导出改成异步导出，张涛负责后端。",
            content="报表导出改成异步导出，张涛负责后端。",
        )
        await TranscriptSegment.create(
            task_id=1,
            meeting_id=self.meeting.id,
            segment_no=2,
            start_ms=5000,
            end_ms=9000,
            speaker_label="B",
            original_text="前端要加一个导出任务列表。",
            content="前端要加一个导出任务列表。",
        )
        self.config = await AiModelConfig.create(
            name="纪要模型",
            model_type="MINUTES",
            base_url="http://localhost",
            api_key="key",
            model_name="qwen-plus",
            enabled=True,
        )
        self.prompt = await PromptTemplate.create(
            code="MINUTES_QA",
            name="问答",
            scene_type="MEETING_MINUTES",
            system_prompt="只依据材料回答",
            user_prompt="{meeting_title}{minutes_json}{segments}{history}{question}",
            enabled=True,
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_answer_with_valid_citation(self):
        mocked = AsyncMock(
            return_value={
                "found": True,
                "answer": "报表导出改为异步导出。",
                "citations": [{"segment_no": 1, "quote": "报表导出改成异步导出，张涛负责后端。"}],
            }
        )
        with patch("services.minutes_qa_service.invoke_json", new=mocked):
            result = await ask(self.minutes, self.config, self.prompt, "报表导出改成什么？")
        self.assertTrue(result["found"])
        self.assertEqual(result["citations"][0]["segment_no"], 1)
        self.assertEqual(result["citations"][0]["speaker"], "张晨")
        self.assertEqual(result["citations"][0]["start_ms"], 0)

    async def test_invalid_citation_is_dropped_and_downgraded(self):
        mocked = AsyncMock(
            return_value={
                "found": True,
                "answer": "编造的回答",
                "citations": [{"segment_no": 99, "quote": "不存在的片段"}],
            }
        )
        with patch("services.minutes_qa_service.invoke_json", new=mocked):
            result = await ask(self.minutes, self.config, self.prompt, "随便问问")
        self.assertFalse(result["found"])
        self.assertEqual(result["citations"], [])
        self.assertIn("未找到足够依据", result["answer"])

    async def test_history_is_truncated(self):
        history = []
        for index in range(5):
            history.append({"role": "user", "content": f"第{index}轮问题"})
            history.append({"role": "assistant", "content": f"第{index}轮回答"})
        text = format_history(history)
        self.assertNotIn("第0轮", text)
        self.assertIn("第4轮", text)

    async def test_citations_are_capped_and_ranked_by_answer_relevance(self):
        by_no = {
            1: {"segment_no": 1, "start_ms": 0, "end_ms": 1000, "speaker": "A", "norm": "决定改为异步导出张涛负责后端"},
            2: {"segment_no": 2, "start_ms": 1000, "end_ms": 2000, "speaker": "B", "norm": "可以加缓存"},
            3: {"segment_no": 3, "start_ms": 2000, "end_ms": 3000, "speaker": "C", "norm": "缓存解决不了任意范围"},
        }
        raw = [
            {"segment_no": 2, "quote": "可以加缓存"},
            {"segment_no": 3, "quote": "缓存解决不了任意范围"},
            {"segment_no": 1, "quote": "决定改为异步导出"},
        ]
        citations = _validate_citations(raw, by_no, "导出怎么改", "决定改为异步导出，张涛负责后端")
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["segment_no"], 1)

    async def test_whitespace_question_rejected(self):
        with self.assertRaises(CustomException) as context:
            await ask_api(self.minutes.id, AskPayload(question="   "), self.owner)
        self.assertIn("请输入问题", context.exception.message)

    async def test_api_permission(self):
        with patch("api.meeting_minutes.ask_minutes", new=AsyncMock(return_value={"found": False, "answer": "无", "citations": []})):
            with self.assertRaises(CustomException) as context:
                await ask_api(self.minutes.id, AskPayload(question="问题"), self.outsider)
            self.assertEqual(context.exception.code, "403")
            result = await ask_api(self.minutes.id, AskPayload(question="问题"), self.owner)
            self.assertEqual(result.code, "200")


if __name__ == "__main__":
    unittest.main()
