"""成本看板：按会议归因、问答单轮成本、缓存命中与未定价计数。"""

from __future__ import annotations

import unittest
from datetime import datetime

from models import (
    AgentRun,
    AiCallLog,
    AiModelConfig,
    ChatMessage,
    ChatSession,
    Meeting,
    MeetingMinutes,
)
from services import observability_service
from tortoise import Tortoise


class CostReportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_cost_by_meeting_and_turn(self):
        config = await AiModelConfig.create(
            name="agent", model_type="AGENT", provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name="m",
            timeout_seconds=60, enabled=True,
            input_price_micro_per_1k=1000, output_price_micro_per_1k=2000)
        meeting = await Meeting.create(
            meeting_no="M1", title="周会",
            start_time=datetime(2026, 10, 19, 9, 0), end_time=datetime(2026, 10, 19, 10, 0),
            creator_id=1, host_id=1, status="FINISHED")
        minutes = await MeetingMinutes.create(
            meeting_id=meeting.id, source_task_id=1, model_config_id=config.id,
            status="CONFIRMED", created_by=1)
        run = await AgentRun.create(
            run_no="A1", minutes_id=minutes.id, meeting_id=meeting.id, user_id=1,
            model_config_id=config.id)
        await AiCallLog.create(
            request_id="r1", call_type="MINUTES", biz_id=minutes.id,
            model_config_id=config.id, model_name="m", request_size=10,
            response_size=10, status="SUCCEEDED", elapsed_ms=10, cost_micro=5000)
        await AiCallLog.create(
            request_id="r2", call_type="AGENT_REVIEW", biz_id=run.id,
            model_config_id=config.id, model_name="m", request_size=10,
            response_size=10, status="SUCCEEDED", elapsed_ms=10, cost_micro=1000)
        session = await ChatSession.create(user_id=1, title="t")
        message = await ChatMessage.create(session_id=session.id, role="ASSISTANT",
                                           content="答", run_status="FINISHED")
        await AiCallLog.create(
            request_id="r3", call_type="CHAT", biz_id=message.id,
            model_config_id=config.id, model_name="m", request_size=10,
            response_size=10, status="SUCCEEDED", elapsed_ms=10, cost_micro=1234,
            usage_json={"input_tokens": 100,
                        "input_token_details": {"cache_read": 40}})
        report = await observability_service.build_cost_report(None, None)
        self.assertEqual(report["by_meeting"][0]["meeting_id"], meeting.id)
        self.assertEqual(report["by_meeting"][0]["cost_micro"], 6000)
        self.assertEqual(report["by_turn"]["turn_count"], 1)
        self.assertEqual(report["by_turn"]["avg_cost_micro"], 1234)
        self.assertEqual(report["cache"]["hit_tokens"], 40)
        self.assertEqual(report["unpriced_count"], 0)
