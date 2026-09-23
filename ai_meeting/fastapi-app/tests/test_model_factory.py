import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
from tortoise import Tortoise

from models import AiCallLog, AiModelConfig
from services import model_factory


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
        with mock.patch.dict("os.environ", {"LLM_MAX_ATTEMPTS": "1"}):
            with self.assertRaises(RuntimeError):
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
