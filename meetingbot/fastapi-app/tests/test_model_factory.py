import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
from langchain_core.messages import AIMessageChunk, HumanMessage
from models import AiCallLog, AiModelConfig
from services import model_factory
from tortoise import Tortoise


class FakeStreamModel:
    """替代 build_tool_model 的返回值：按脚本依次吐 token 或抛异常。

    字符串包成真实的 AIMessageChunk，并给最后一段带上 usage——
    形状与真实网关一致（用量在最后一个 chunk 上）。
    """

    DEFAULT_USAGE = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}

    def __init__(self, chunks, usage=None):
        self.usage = self.DEFAULT_USAGE if usage is None else usage
        self.chunks = []
        for item in chunks:
            if isinstance(item, (AIMessageChunk, Exception)):
                self.chunks.append(item)
            else:
                self.chunks.append(AIMessageChunk(content=item))
        if self.usage and self.chunks and isinstance(self.chunks[-1], AIMessageChunk):
            self.chunks[-1] = self.chunks[-1].model_copy(update={"usage_metadata": self.usage})
        self.attempts = 0

    async def astream(self, messages):
        """按脚本吐内容；异常抛出即停止，重试从剩下的脚本接着走。"""
        self.attempts += 1
        while self.chunks:
            item = self.chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            if self.usage and isinstance(item, AIMessageChunk) and item.usage_metadata is None:
                item = item.model_copy(update={"usage_metadata": self.usage})
            yield item


class FakeChatModel:
    """替代 build_chat_model 的返回值：按脚本依次抛异常或返回成功消息。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.calls += 1
        self.prompts.append(messages[1].content)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok_message() -> SimpleNamespace:
    return SimpleNamespace(text='{"answer": 42}', usage_metadata={"total_tokens": 7})


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=httpx.Request("POST", "https://api.test/v1/chat"))


class InvokeJsonRetryTest(unittest.IsolatedAsyncioTestCase):
    """瞬态重试：每次尝试各留一条日志（一行日志 = 一次真实调用），非瞬态不重试。"""

    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.config = await AiModelConfig.create(
            name="test", model_type="AGENT", base_url="https://api.test/v1",
            api_key="key", model_name="test-model")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _invoke(self, outcomes):
        fake = FakeChatModel(outcomes)
        with mock.patch.object(model_factory, "build_chat_model", return_value=fake), \
                mock.patch.object(model_factory.asyncio, "sleep", new=self._no_sleep):
            result = await model_factory.invoke_json(
                self.config, "system", "user", {"type": "object"}, "AGENT_REVIEW",
                1, "自检模型")
        return result, fake, fake.calls

    @staticmethod
    async def _no_sleep(_seconds):
        return None

    async def test_transient_failure_is_retried_and_each_attempt_logged(self):
        result, fake, calls = await self._invoke([timeout_error(), timeout_error(), ok_message()])
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(calls, 3)
        logs = await AiCallLog.all().order_by("id")
        self.assertEqual(len(logs), 3)
        self.assertEqual([log.status for log in logs], ["FAILED", "FAILED", "SUCCEEDED"])
        # 重试开启时失败行带尝试序号，观测页能看出哪些失败后面跟了重试
        self.assertIn("[尝试 1/3]", logs[0].error_message)
        self.assertIn("[尝试 2/3]", logs[1].error_message)

    async def test_invalid_output_is_reasked_with_error_fed_back(self):
        """instructor 式 re-ask：解析/校验失败不白花钱——错误回注下一次提示词再试。"""
        result, fake, calls = await self._invoke([ValueError("Invalid JSON"), ok_message()])
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(calls, 2)
        logs = await AiCallLog.all().order_by("id")
        self.assertEqual([log.status for log in logs], ["FAILED", "SUCCEEDED"])
        # 第二次调用的提示词必须带上一次的错误（re-ask 附注）
        second_prompt = fake.prompts[1]
        self.assertIn("上一次输出不合格", second_prompt)
        self.assertIn("Invalid JSON", second_prompt)

    async def test_invalid_output_exhausted_attempts_raises(self):
        outcomes = [ValueError("Invalid JSON")] * 3
        with self.assertRaises(RuntimeError):
            await self._invoke(outcomes)
        logs = await AiCallLog.all()
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(log.status == "FAILED" for log in logs))

    async def test_transient_failures_exhausted_raises_after_all_attempts(self):
        with self.assertRaises(RuntimeError):
            await self._invoke([timeout_error(), timeout_error(), timeout_error(), ok_message()])
        logs = await AiCallLog.all()
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(log.status == "FAILED" for log in logs))

    async def test_retry_disabled_with_single_attempt(self):
        with mock.patch.dict("os.environ", {"LLM_MAX_ATTEMPTS": "1"}), self.assertRaises(RuntimeError):
            await self._invoke([timeout_error(), ok_message()])
        logs = await AiCallLog.all()
        self.assertEqual(len(logs), 1)
        # 只有一次尝试时不加尝试序号前缀，失败信息与旧行为一致
        self.assertNotIn("[尝试", logs[0].error_message)


class IsTransientTest(unittest.TestCase):
    """瞬态判定的边界：截断与解析错误不算瞬态。"""

    def test_truncation_is_not_transient(self):
        completion = SimpleNamespace(text="...truncated", usage=None)
        error = openai.LengthFinishReasonError(completion=completion)
        self.assertFalse(model_factory.is_transient(error))

    def test_server_error_and_timeout_are_transient(self):
        self.assertTrue(model_factory.is_transient(timeout_error()))
        self.assertTrue(model_factory.is_transient(
            openai.APIConnectionError(message="connection reset", request=httpx.Request("GET", "https://x"))))

    def test_plain_value_error_is_not_transient(self):
        self.assertFalse(model_factory.is_transient(ValueError("bad json")))


if __name__ == "__main__":
    unittest.main()


class StructuredOutputBindTest(unittest.TestCase):
    """build_chat_model 的两种结构化输出策略：strict json_schema 与 json_object。"""

    def _config(self, base_url):
        return AiModelConfig(name="t", model_type="MINUTES", base_url=base_url,
                             api_key="k", model_name="m")

    def test_json_schema_provider_binds_strict_schema(self):
        model = model_factory.build_chat_model(
            self._config("https://dashscope.aliyuncs.com/compatible-mode/v1"),
            {"type": "object"}, "minutes")
        kwargs = model.kwargs
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])
        self.assertEqual(kwargs["response_format"]["json_schema"]["name"], "minutes")

    def test_json_object_provider_only_declares_object(self):
        model = model_factory.build_chat_model(
            self._config("https://api.deepseek.com"), {"type": "object"}, "minutes")
        self.assertEqual(model.kwargs["response_format"], {"type": "json_object"})

    def test_strategy_follows_host_suffix(self):
        from common import providers
        self.assertEqual(providers.structured_output_strategy(
            "https://dashscope.aliyuncs.com/compatible-mode/v1"), providers.JSON_SCHEMA)
        self.assertEqual(providers.structured_output_strategy(
            "https://api.deepseek.com"), providers.JSON_OBJECT)


class ChatStreamTest(unittest.IsolatedAsyncioTestCase):
    """流式问答调用：token 逐段产出、记账一行一次调用、失败分类与中断留痕。"""

    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["models"]},
            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.config = await AiModelConfig.create(
            name="chat", model_type="AGENT", base_url="https://api.test/v1",
            api_key="key", model_name="test-chat")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    async def _stream(self, chunks, usage=None, **kwargs):
        fake = FakeStreamModel(chunks, usage=usage)
        events = []
        with mock.patch.object(model_factory.asyncio, "sleep", new=self._no_sleep):
            async for kind, payload in model_factory.stream_chat(
                    self.config, [HumanMessage("问题")], model=fake, biz_id=7, **kwargs):
                events.append((kind, payload))
        return events, fake

    async def _no_sleep(self, _seconds):
        return None

    async def test_tokens_are_streamed_and_message_is_returned(self):
        events, fake = await self._stream(["上线", "时间", "定在十月"])
        self.assertEqual([payload for kind, payload in events if kind == "token"],
                         ["上线", "时间", "定在十月"])
        message = events[-1][1]
        self.assertEqual(message.content, "上线时间定在十月")
        self.assertEqual(events[-1][0], "message")

    async def test_each_call_writes_exactly_one_log_row_with_usage(self):
        await self._stream(["答案"])
        logs = await AiCallLog.all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, "SUCCEEDED")
        self.assertEqual(logs[0].call_type, model_factory.CHAT_CALL_TYPE)
        self.assertEqual(logs[0].biz_id, 7)
        self.assertEqual(logs[0].usage_json.get("total_tokens"), 30)

    async def test_transient_failure_before_first_token_is_retried(self):
        chunks = [timeout_error(), "重试成功"]
        events, fake = await self._stream(chunks)
        self.assertEqual(fake.attempts, 2)
        self.assertEqual(events[-1][1].content, "重试成功")
        statuses = [row.status for row in await AiCallLog.all().order_by("id")]
        self.assertEqual(statuses, ["FAILED", "SUCCEEDED"])

    async def test_failure_after_first_token_is_not_retried(self):
        chunks = ["半截回答", timeout_error()]
        with self.assertRaises(RuntimeError):
            await self._stream(chunks)
        rows = await AiCallLog.all().order_by("id")
        self.assertEqual([row.status for row in rows], ["FAILED"])
        # 已吐字的失败不重试：日志里只有第一次尝试
        self.assertIn("尝试 1/", rows[0].error_message)

    async def test_client_interrupt_still_leaves_a_failed_row(self):
        fake = FakeStreamModel(["很长的回答", "还有更多", "仍有更多"])
        stream = model_factory.stream_chat(self.config, [HumanMessage("问")], model=fake)
        first = await stream.__anext__()
        self.assertEqual(first[0], "token")
        await stream.aclose()
        rows = await AiCallLog.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "FAILED")
        self.assertIn("客户端中断", rows[0].error_message)

    async def test_missing_usage_is_marked_as_estimated(self):
        from langchain_core.messages import AIMessageChunk
        events, _ = await self._stream([AIMessageChunk(content="没有用量")], usage=False)
        usage = (await AiCallLog.all())[0].usage_json
        self.assertTrue(usage["estimated"])
        self.assertGreater(usage["total_tokens"], 0)
        self.assertEqual(events[-1][1].content, "没有用量")


class UsageEstimateTest(unittest.TestCase):
    def test_real_usage_is_used_when_present(self):
        from langchain_core.messages import AIMessage
        usage = model_factory.usage_or_estimate(
            AIMessage(content="x", usage_metadata={"input_tokens": 30, "output_tokens": 12,
                                                   "total_tokens": 42}), 100)
        self.assertEqual(usage["total_tokens"], 42)
        self.assertNotIn("estimated", usage)

    def test_absent_usage_falls_back_to_characters_and_says_so(self):
        from langchain_core.messages import AIMessage
        usage = model_factory.usage_or_estimate(AIMessage(content="四个字"), 10)
        self.assertEqual(usage["total_tokens"], 13)
        self.assertTrue(usage["estimated"])


class ToolBindingTest(unittest.TestCase):
    def _config(self, base_url):
        return AiModelConfig(name="t", model_type="AGENT", base_url=base_url,
                             api_key="k", model_name="m")

    def test_declared_provider_binds_tools(self):
        tools = [{"type": "function", "function": {"name": "search_knowledge",
                                                   "description": "d", "parameters": {}}}]
        model = model_factory.build_tool_model(
            self._config("https://api.deepseek.com"), tools, "chat")
        self.assertEqual(model.kwargs["tools"][0]["function"]["name"], "search_knowledge")

    def test_undeclared_provider_fails_before_the_request(self):
        with self.assertRaises(RuntimeError) as ctx:
            model_factory.build_tool_model(
                self._config("https://nobody-heard-of.example/v1"), [], "chat")
        self.assertIn("KNOWN_TOOL_CALLING", str(ctx.exception))

    def test_chat_does_not_request_reasoning_by_default(self):
        from common import providers
        self.assertFalse(providers.wants_reasoning("chat"))
        self.assertFalse(providers.wants_reasoning("chat_summary"))


class EmbeddingShapeTest(unittest.IsolatedAsyncioTestCase):
    """嵌入结果取向量的字段名：这是只有真调用才会暴露的一类错误。

    实测：`item.vector` 在打桩测试里全绿（桩的返回形状是测试自己编的），
    真跑百炼 compatible-mode 报 `'Embedding' object has no attribute 'vector'`
    ——OpenAI 兼容协议的字段叫 `embedding`。所以这里按**协议形状**构造桩。
    """

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.config = await AiModelConfig.create(
            name="嵌入", model_type="EMBEDDING", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="k", model_name="text-embedding-v4", enabled=True)

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def _client(self, items):
        class _Embeddings:
            async def create(self, **_kwargs):
                return SimpleNamespace(data=items, usage={"prompt_tokens": 3, "total_tokens": 3})
        return SimpleNamespace(embeddings=_Embeddings())

    async def test_openai_compatible_embedding_field_is_used(self):
        from types import SimpleNamespace
        items = [SimpleNamespace(index=0, embedding=[0.1] * 4),
                 SimpleNamespace(index=1, embedding=[0.2] * 4)]
        with mock.patch.object(model_factory, "embedding_dim", return_value=4):
            vectors = await model_factory.embed_texts(
                self.config, ["甲", "乙"], biz_id=0, client=self._client(items))
        self.assertEqual(vectors, [[0.1] * 4, [0.2] * 4])
        rows = await AiCallLog.all()
        self.assertEqual(rows[0].status, "SUCCEEDED")

    async def test_legacy_vector_field_still_accepted(self):
        from types import SimpleNamespace
        items = [SimpleNamespace(index=0, vector=[0.5] * 4)]
        with mock.patch.object(model_factory, "embedding_dim", return_value=4):
            vectors = await model_factory.embed_texts(
                self.config, ["甲"], biz_id=0, client=self._client(items))
        self.assertEqual(vectors, [[0.5] * 4])

    async def test_missing_vector_field_reports_readably(self):
        from types import SimpleNamespace
        items = [SimpleNamespace(index=0)]
        with mock.patch.object(model_factory, "embedding_dim", return_value=4), \
                self.assertRaises(RuntimeError) as ctx:
            await model_factory.embed_texts(
                self.config, ["甲"], biz_id=0, client=self._client(items))
        self.assertIn("没有向量字段", str(ctx.exception))
