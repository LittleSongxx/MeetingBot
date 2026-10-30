"""演示数据幂等与“删除后重启补播种”回归测试。

覆盖两个已修复的坑：
1. demo_seed 固定 request_id 撞唯一索引导致 bootstrap 崩溃；
2. 删除演示转写任务/纪要/自检运行后，再次播种应能补回且不抛 IntegrityError。
"""

import unittest

from demo_seed import DEMO_RUN_NO, seed_ai_demo
from models import (
    ActionItem,
    AgentRun,
    AgentStep,
    AiCallLog,
    AiModelConfig,
    Meeting,
    MeetingMaterial,
    MeetingMinutes,
    TranscriptionTask,
    TranscriptSegment,
    User,
)
from tortoise import Tortoise


class DemoSeedIdempotentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.host = await User.create(
            username="employee", password="x", name="张晨", role="EMPLOYEE"
        )
        self.operator = await User.create(
            username="operator", password="x", name="李悦", role="EMPLOYEE"
        )
        self.meeting = await Meeting.create(
            meeting_no="MTG-DEMO-001",
            title="产品发布评审会",
            start_time="2026-10-30 09:00:00",
            end_time="2026-10-30 10:00:00",
            creator_id=self.host.id,
            host_id=self.host.id,
        )
        self.material = await MeetingMaterial.create(
            meeting_id=self.meeting.id,
            file_name="产品发布评审会议录音.mp3",
            storage_name="demo-audio.mp3",
            file_type="AUDIO",
            file_ext="mp3",
            file_size=1024,
            file_hash="demo-hash-audio",
            uploader_id=self.host.id,
        )
        for model_type in ("TRANSCRIPTION", "MINUTES", "AGENT", "SPEAKER"):
            await AiModelConfig.create(
                name=f"demo-{model_type}",
                model_type=model_type,
                provider="OPENAI_COMPATIBLE",
                base_url="http://localhost",
                api_key="test-key",
                model_name="qwen-plus",
                timeout_seconds=60,
                enabled=True,
            )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _seed(self):
        await seed_ai_demo(self.meeting, self.host, self.operator)

    async def _counts(self):
        return (
            await TranscriptionTask.all().count(),
            await TranscriptSegment.all().count(),
            await MeetingMinutes.all().count(),
            await AgentRun.all().count(),
            await AgentStep.all().count(),
            await AiCallLog.all().count(),
        )

    async def test_seed_twice_is_idempotent(self):
        await self._seed()
        first = await self._counts()
        await self._seed()
        second = await self._counts()
        self.assertEqual(first, second)
        # 1 任务 / 14 片段 / 1 纪要 / 1 运行 / 4 步骤 / 10 条调用日志
        self.assertEqual(first, (1, 14, 1, 1, 4, 10))
        demo_run = await AgentRun.get(run_no=DEMO_RUN_NO)
        self.assertEqual(demo_run.status, "WAITING_CONFIRMATION")
        # 演示纪要带 evidence 和 AI 草稿；同时补 3 条演示行动项
        demo_minutes = await MeetingMinutes.get()
        self.assertIsNotNone(demo_minutes.ai_draft_json)
        self.assertTrue(demo_minutes.decisions_json[0].get("evidence"))
        self.assertEqual(await ActionItem.all().count(), 3)
        self.assertEqual(await ActionItem.filter(status="OPEN").count(), 1)
        self.assertEqual(await ActionItem.filter(status="DONE").count(), 1)

    async def test_manual_edit_is_not_overwritten_on_reseed(self):
        await self._seed()
        minutes = await MeetingMinutes.get()
        await MeetingMinutes.filter(id=minutes.id).update(summary="人工改过的摘要")
        await self._seed()
        self.assertEqual((await MeetingMinutes.get(id=minutes.id)).summary, "人工改过的摘要")

    async def test_reseed_after_demo_rows_deleted(self):
        await self._seed()
        # 模拟管理员在页面删除演示转写任务（级联删纪要/自检），调用日志按设计保留
        await ActionItem.all().delete()
        await AgentStep.all().delete()
        await AgentRun.all().delete()
        await MeetingMinutes.all().delete()
        await TranscriptSegment.all().delete()
        await TranscriptionTask.all().delete()
        self.assertEqual(await AiCallLog.all().count(), 10)
        # 再次启动 bootstrap：必须能补回演示数据，且不撞 ai_call_log.request_id 唯一索引
        await self._seed()
        self.assertEqual(await self._counts(), (1, 14, 1, 1, 4, 10))
        task = await TranscriptionTask.get()
        minutes = await MeetingMinutes.get()
        run = await AgentRun.get()
        self.assertEqual(minutes.source_task_id, task.id)
        self.assertEqual(run.minutes_id, minutes.id)
        # 固定 request_id 的日志会被原地更新到新业务行，不会产生指向已删数据的孤儿
        log = await AiCallLog.get(request_id="AICDEMO0001")
        self.assertEqual(log.biz_id, task.id)
        self.assertEqual(await ActionItem.all().count(), 3)


if __name__ == "__main__":
    unittest.main()
