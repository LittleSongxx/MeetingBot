"""可观测性的单测：JSON 日志、指标口径、OTLP 默认关闭。

三个断言各对应一条容易悄悄失效的性质：
- 日志是**一行一条 JSON**（多行堆栈会让日志采集器按行切错）；
- 指标名与 OTel 语义约定对得上，且**失败调用也计入耗时**（只统计成功会让"变慢"
  与"变错"混在一起看不出）；
- 没配 OTLP 端点时不联网、不报错，span 照常可创建（默认关闭不等于不能用）。
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from unittest import mock

from common import metrics, tracing


class JsonLogTest(unittest.TestCase):
    def _record(self, message: str = "发生了点事", *, extra: dict | None = None,
                exc: Exception | None = None) -> str:
        record = logging.LogRecord(
            name="meetingbot.test", level=logging.WARNING, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=(type(exc), exc, None) if exc else None)
        for key, value in (extra or {}).items():
            record.__dict__[key] = value
        return metrics.JsonFormatter().format(record)

    def test_one_line_json(self):
        line = self._record("多行\n消息")
        self.assertEqual(len(line.splitlines()), 1)
        payload = json.loads(line)
        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["logger"], "meetingbot.test")
        self.assertIn("多行\n消息", payload["message"])

    def test_extra_fields_go_under_extra(self):
        payload = json.loads(self._record(extra={"request_id": "AIC1", "model": "m"}))
        self.assertEqual(payload["extra"], {"request_id": "AIC1", "model": "m"})

    def test_exception_is_flattened_into_the_payload(self):
        try:
            raise ValueError("坏值")
        except ValueError as error:
            payload = json.loads(self._record("失败", exc=error))
        self.assertIn("ValueError", payload["exception"])
        self.assertEqual(len(json.dumps(payload).splitlines()), 1)

    def test_setup_logging_installs_one_handler_and_json_format(self):
        with mock.patch.dict(os.environ, {"LOG_FORMAT": "json", "LOG_LEVEL": "DEBUG"}):
            metrics.setup_logging()
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1)
        self.assertIsInstance(root.handlers[0].formatter, metrics.JsonFormatter)
        self.assertEqual(root.level, logging.DEBUG)
        # uvicorn 自带的 handler 被清掉并改为向上冒泡（否则日志会打两份）
        self.assertEqual(logging.getLogger("uvicorn.error").handlers, [])
        self.assertTrue(logging.getLogger("uvicorn.error").propagate)

    def test_text_format_is_available_for_local_development(self):
        with mock.patch.dict(os.environ, {"LOG_FORMAT": "text"}):
            metrics.setup_logging()
        formatter = logging.getLogger().handlers[0].formatter
        self.assertNotIsInstance(formatter, metrics.JsonFormatter)
        # 收尾：恢复 JSON（其它测试可能按它断言）
        metrics.setup_logging()


class MetricTest(unittest.TestCase):
    def setUp(self):
        # 不断言"总数"而只断言"存在哪些序列"：Prometheus 的 Counter 是进程级的，
        # 用例之间会累加，断言总数等于把测试顺序变成隐式依赖
        self.render = lambda: metrics.render().decode("utf-8")

    def test_names_follow_the_semantic_convention(self):
        metrics.record_model_call(model="m1", call_type="CHAT", status="SUCCEEDED",
                                  elapsed_ms=1200, input_tokens=100, output_tokens=50,
                                  cost_micro=700, priced=True)
        text = self.render()
        # OTel 语义约定名带点号，Prometheus 不允许，映射关系写在模块文档里
        self.assertIn("gen_ai_client_token_usage_total", text)
        self.assertIn('call_type="CHAT"', text)
        self.assertIn('type="input"', text)
        self.assertIn("gen_ai_client_operation_duration_seconds_bucket", text)
        self.assertIn("meetingbot_ai_call_cost_micro_total", text)

    def test_failed_calls_are_counted_in_duration(self):
        metrics.record_model_call(model="m1", call_type="CHAT", status="FAILED", elapsed_ms=500)
        text = self.render()
        self.assertIn('status="FAILED"', text)
        # 失败不产生 token 与成本计数（没有真实用量，编一个数是假数据）
        self.assertNotIn('type="input",model="m1",call_type="CHAT"} 1', text)

    def test_unpriced_calls_are_counted_separately(self):
        metrics.record_model_call(model="m2", call_type="CHAT", status="SUCCEEDED",
                                  elapsed_ms=100, input_tokens=10, output_tokens=5,
                                  cost_micro=0, priced=False)
        text = self.render()
        self.assertIn("meetingbot_ai_call_unpriced_total", text)
        # 未定价的调用不进成本计数器：否则面板上会显示"成本 0"，看起来像免费
        self.assertNotIn('meetingbot_ai_call_cost_micro_total{call_type="CHAT",model="m2"} ', text)


class TracingTest(unittest.TestCase):
    def test_disabled_without_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            self.assertFalse(tracing.enabled())

    def test_span_is_usable_even_when_export_is_off(self):
        # 默认实现是 no-op，但 span 本身必须能开、能设属性、不抛异常
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            with tracing.span("gen_ai.chat", tracing.model_attributes(
                    call_type="CHAT", model="m", base_url="https://api.deepseek.com")) as current:
                current.set_attribute("gen_ai.usage.input_tokens", 10)
        self.assertIsNotNone(current)

    def test_setup_is_idempotent_and_reports_state(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            self.assertFalse(tracing.setup_tracing())
            self.assertFalse(tracing.setup_tracing())

    def test_attribute_names_follow_the_convention(self):
        attributes = tracing.model_attributes(call_type="AGENT_REVIEW", model="deepseek-flash",
                                              base_url="https://api.deepseek.com/v1")
        self.assertEqual(attributes["gen_ai.system"], "api.deepseek.com")
        self.assertEqual(attributes["gen_ai.operation.name"], "agent_review")
        self.assertEqual(attributes["gen_ai.request.model"], "deepseek-flash")
        usage = tracing.usage_attributes(input_tokens=1, output_tokens=2, cost_micro=3)
        self.assertEqual(usage["gen_ai.usage.input_tokens"], 1)
        self.assertEqual(usage["meetingbot.cost_micro"], 3)


class PercentileTest(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        from services.observability_service import percentile

        self.assertEqual(percentile([], 95), 0)
        self.assertEqual(percentile([10], 95), 10)
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 5], 95), 5)
        self.assertEqual(percentile([5, 1, 3], 50), 3)



if __name__ == "__main__":
    unittest.main()
