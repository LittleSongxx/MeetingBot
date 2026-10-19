"""前缀缓存的前提：系统提示稳定、结构顺序固定、问题不进系统前缀。

供应商的自动前缀缓存命中要求请求前缀逐字节相同。问答每轮只有用户问题与历史
在变，系统提示必须保持稳定；这里把"稳定"变成可回归的断言，避免以后有人把
时间戳、会话 id、随机内容拼进 system prompt 而悄悄让缓存失效。
"""

from __future__ import annotations

import unittest

from services import chat_prompts, prompt_injection
from tortoise import Tortoise


class PromptPrefixTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
        await Tortoise.generate_schemas()

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def test_system_prompt_is_stable_and_ordered(self):
        first = await chat_prompts.get_system_prompt()
        second = await chat_prompts.get_system_prompt()
        self.assertEqual(first, second, "系统提示必须在同一配置下逐字节稳定")
        start = chat_prompts.DEFAULT_SYSTEM_PROMPT.strip()
        self.assertTrue(first.startswith(start))
        positions = [
            first.index(chat_prompts.SOURCE_COVERAGE_PROTOCOL),
            first.index(chat_prompts.STATE_PROTOCOL),
            first.index(prompt_injection.system_guards()),
            first.index(chat_prompts.CITATION_PROTOCOL),
        ]
        self.assertEqual(positions, sorted(positions), "协议块顺序必须固定")

    async def test_question_is_not_in_system_prefix(self):
        system = await chat_prompts.get_system_prompt()
        question = "上周发布定了什么"
        self.assertNotIn(question, system)
        message = await chat_prompts.question_message(question)
        self.assertIn(question, message.content)
