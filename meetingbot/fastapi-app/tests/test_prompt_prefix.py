"""前缀缓存的前提：系统提示稳定、结构顺序固定、问题不进系统前缀。

供应商的自动前缀缓存命中要求请求前缀逐字节相同。问答每轮只有用户问题与历史
在变，系统提示必须保持稳定；这里把"稳定"变成可回归的断言，避免以后有人把
时间戳、会话 id、随机内容拼进 system prompt 而悄悄让缓存失效。
"""

from __future__ import annotations

import unittest

from models import PromptTemplate
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
            first.index(chat_prompts.TOOL_CALLING_PROTOCOL),
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

    async def test_tool_call_discipline_is_in_the_system_prompt(self):
        """首轮并行、工具轮不输出正文、拿到结果即回答：三条都是首 token 延迟与轮次成本约定。"""
        system = await chat_prompts.get_system_prompt()
        protocol = chat_prompts.TOOL_CALLING_PROTOCOL
        self.assertIn(protocol, system)
        # 1) 首轮互相独立的工具一次并发发完，不要一轮只发一个
        self.assertIn("首轮", protocol)
        self.assertIn("互相独立", protocol)
        self.assertIn("同一轮", protocol)
        self.assertIn("并发发起全部工具调用", protocol)
        self.assertIn("不要一轮只发一个", protocol)
        # 2) 要调工具的轮次只输出工具调用，不要解释性正文/开场白
        self.assertIn("只输出工具调用", protocol)
        self.assertIn("解释性正文", protocol)
        # 3) 槽位齐全就直接回答，不为同一事实重复检索；缺槽位才继续检索
        self.assertIn("关键槽位", protocol)
        self.assertIn("直接回答", protocol)
        self.assertIn("不要为同一事实重复检索", protocol)
        for slot in ("负责人", "截止时间", "数量", "时间点"):
            self.assertIn(slot, protocol)

    async def test_tool_call_discipline_survives_admin_template(self):
        """工具调用纪律是结构性约束：管理员改模板正文也关不掉它。"""
        await PromptTemplate.create(
            code="CHAT_ANSWER", name="问答", scene_type="CHAT",
            system_prompt="你是定制助手。", user_prompt="")
        system = await chat_prompts.get_system_prompt()
        self.assertTrue(system.startswith("你是定制助手。"))
        self.assertIn(chat_prompts.TOOL_CALLING_PROTOCOL, system)
