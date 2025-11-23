"""模型路由：有路由走路由、失效回退原用途、默认空表行为不变。"""

from __future__ import annotations

import unittest

from models import AiModelConfig, ModelRoute
from services import model_config
from tortoise import Tortoise


class ModelRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _config(self, model_type: str, name: str) -> AiModelConfig:
        return await AiModelConfig.create(
            name=name, model_type=model_type, provider="OPENAI_COMPATIBLE",
            base_url="https://example.com/v1", api_key="k", model_name=name,
            timeout_seconds=60, enabled=True,
        )

    async def test_route_wins_and_falls_back(self):
        agent = await self._config("AGENT", "strong")
        cheap = await self._config("CHEAP", "cheap")
        route = await ModelRoute.create(route_key="CHAT", model_config_id=cheap.id,
                                       enabled=True)
        self.assertEqual((await model_config.resolve_route("CHAT", "AGENT")).id, cheap.id)
        await ModelRoute.filter(id=route.id).update(enabled=False)
        self.assertEqual((await model_config.resolve_route("CHAT", "AGENT")).id, agent.id)

    async def test_disabled_target_falls_back(self):
        agent = await self._config("AGENT", "strong")
        cheap = await self._config("CHEAP", "cheap")
        await ModelRoute.create(route_key="CHAT_SUMMARY", model_config_id=cheap.id,
                                enabled=True)
        await AiModelConfig.filter(id=cheap.id).update(enabled=False)
        self.assertEqual(
            (await model_config.resolve_route("CHAT_SUMMARY", "AGENT")).id, agent.id)
