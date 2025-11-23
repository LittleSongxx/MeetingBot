"""在线评测：采样、判定、汇总；判定调用全部 mock，零模型调用。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from models import AiModelConfig, ChatMessage, ChatSession, OnlineEvalCase, OnlineEvalRun
from services import online_eval_service
from tortoise import Tortoise


class OnlineEvalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_run_scores_and_summary(self):
        config = await AiModelConfig.create(
            name="judge", model_type="JUDGE", provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name="judge",
            timeout_seconds=60, enabled=True)
        session = await ChatSession.create(user_id=1, title="t", last_active_time=None)
        await ChatMessage.create(session_id=session.id, role="USER", content="发布范围是什么")
        message = await ChatMessage.create(
            session_id=session.id, role="ASSISTANT", content="发布范围包含全生命周期",
            run_status="FINISHED",
            citations_json={"used": [{"ref": 1, "quote": "发布范围包含全生命周期"}]})
        run = await OnlineEvalRun.create(
            triggered_by=1, sample_size=5, model_config_id=config.id)
        with patch.object(online_eval_service, "_judge_config",
                          AsyncMock(return_value=config)), \
                patch.object(online_eval_service.model_factory, "invoke_json",
                             AsyncMock(return_value={
                                 "faithfulness": 0.9, "answer_relevancy": 0.8,
                                 "evidence": "引用支撑"})):
            await online_eval_service.run_online_eval(run.id)
        stored = await OnlineEvalRun.get(id=run.id)
        self.assertEqual(stored.status, "DONE")
        self.assertEqual(stored.summary_json["case_count"], 1)
        self.assertAlmostEqual(stored.summary_json["avg_faithfulness"], 0.9)
        self.assertEqual(stored.summary_json["pass_rate"], 1.0)
        cases = await OnlineEvalCase.filter(run_id=run.id)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].chat_message_id, message.id)
