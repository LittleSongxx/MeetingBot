"""在线问答 checkpointer 的零调用测试：编译、落 checkpoint、从同 thread 恢复。"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from services import chat_checkpoint, chat_graph
from services.chat_runtime import ChatRuntime, reset_current, set_current

PG_TEST_DSN = (os.getenv("PG_TEST_DSN") or "").strip()


class CheckpointGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_thread_resumes_without_recalling_model(self):
        calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            return AIMessage(content="已从 checkpoint 恢复")

        runtime = ChatRuntime(
            call_model=call_model,
            tools=None,
            cost_micro_of=lambda usage: 0,
        )
        graph = chat_graph.build_chat_graph(chat_checkpoint.in_memory_checkpointer())
        config = {"configurable": {"thread_id": "test-turn-1", "runtime": runtime}}
        state = {"messages": [HumanMessage(content="问题")]}
        token = set_current(runtime)
        try:
            first = await graph.ainvoke(state, config=config)
            self.assertEqual(calls, 1)
            self.assertEqual(chat_graph.final_answer(first), "已从 checkpoint 恢复")

            # 传 None 表示从最近 checkpoint 继续；图已在 END，不应再调模型
            second = await graph.ainvoke(None, config=config)
            self.assertEqual(calls, 1)
            self.assertEqual(chat_graph.final_answer(second), "已从 checkpoint 恢复")
        finally:
            reset_current(token)


@unittest.skipUnless(PG_TEST_DSN, "需要 PG_TEST_DSN（ParadeDB PostgreSQL）")
class PostgresCheckpointTest(unittest.IsolatedAsyncioTestCase):
    """在真实 PG 上落 checkpoint：验证 schema/setup 与跨实例恢复，而不是只用 InMemorySaver。"""

    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"CHAT_CHECKPOINT_ENABLED": "1"})
        self._env.start()
        if not await chat_checkpoint.init(PG_TEST_DSN):
            # 显式给了 DSN 却初始化失败，CI 必须红；skip 会掩盖 schema/权限问题
            self.fail("PostgreSQL checkpoint 初始化失败（检查 PG_TEST_DSN/权限）")

    async def asyncTearDown(self):
        await chat_checkpoint.close()
        self._env.stop()

    async def test_round_trip_on_postgres(self):
        calls = 0

        async def call_model(messages):
            nonlocal calls
            calls += 1
            return AIMessage(content="PG checkpoint 已恢复")

        runtime = ChatRuntime(call_model=call_model, tools=None,
                              cost_micro_of=lambda usage: 0)
        graph = chat_graph.build_chat_graph(chat_checkpoint.get_checkpointer())
        config = {"configurable": {"thread_id": f"test-pg-{uuid.uuid4()}",
                                  "runtime": runtime}}
        state = {"messages": [HumanMessage(content="问题")]}
        token = set_current(runtime)
        try:
            first = await graph.ainvoke(state, config=config)
            second = await graph.ainvoke(None, config=config)
        finally:
            reset_current(token)
        self.assertEqual(calls, 1)
        self.assertEqual(chat_graph.final_answer(first), "PG checkpoint 已恢复")
        self.assertEqual(chat_graph.final_answer(second), "PG checkpoint 已恢复")


if __name__ == "__main__":
    unittest.main()
