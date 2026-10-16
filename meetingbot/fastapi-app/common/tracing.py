"""链路追踪：按 OTel GenAI 语义约定发 span，OTLP 导出**默认关闭**。

## 为什么默认关闭

Span 的价值在"有后端能看"，而导出后端（collector / Jaeger / 云 APM）是部署方
才有的事实。因此这里不内置任何导出地址、也不默认联网上报：没配
`OTEL_EXPORTER_OTLP_ENDPOINT` 时用 OTel API 的默认实现——**span 照发、但直接丢弃**，
调用点一行都不用改；配了地址就装上 SDK 与 OTLP 导出器开始上报。

## 为什么用 OTel API 而不是自己写一层

属性名（`gen_ai.*`）是跨厂商的语义约定，面板、告警与别人的工具都按这套认字段。
自己定义一套字段名等于把"可观测"做成只有本项目看得懂的私有格式。这里只做两件事：
把本项目已有的量（模型名、调用类型、token、耗时、成本）映射到约定字段，
以及把开关收敛到一个环境变量。

属性名取自 OTel GenAI 语义约定（稳定版字段）：
`gen_ai.system` / `gen_ai.operation.name` / `gen_ai.request.model` /
`gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`。
成本不是语义约定里的字段（约定未定义价格），用 `meetingbot.cost_micro` 前缀区分。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("meetingbot.tracing")

_installed = False


def enabled() -> bool:
    """是否配置了 OTLP 导出端点。未配置时 span 仍然可以创建（只是不会被导出）。"""
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def setup_tracing() -> bool:
    """按环境变量装一次追踪。返回是否真的装了导出器（供启动日志与测试断言）。

    幂等：重复调用不再装第二套 SDK（第二次会报"TracerProvider 已设置"，
    而且那种报错会让人以为追踪坏了）。
    """
    global _installed
    if _installed:
        return enabled()
    _installed = True
    if not enabled():
        logger.info("OTLP 导出未启用（未设置 OTEL_EXPORTER_OTLP_ENDPOINT），span 只在本进程内生成")
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.getenv("OTEL_SERVICE_NAME", "meetingbot-backend")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    logger.info("OTLP 导出已启用：service.name=%s", service_name)
    return True


def tracer() -> Any:
    from opentelemetry import trace
    return trace.get_tracer("meetingbot")


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """开一个 span。异常会照常向上抛，同时被记进 span 状态（不吞异常、不改变控制流）。"""
    with tracer().start_as_current_span(name) as current:
        for key, value in (attributes or {}).items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def model_attributes(*, call_type: str, model: str, base_url: str = "") -> dict[str, Any]:
    """一次模型调用的 span 属性。`gen_ai.system` 取供应商主机名（约定要求供应商标识）。"""
    from common import providers
    return {
        "gen_ai.system": providers.host_of(base_url) or "unknown",
        "gen_ai.operation.name": (call_type or "unknown").lower(),
        "gen_ai.request.model": model or "unknown",
    }


def usage_attributes(*, input_tokens: int = 0, output_tokens: int = 0,
                     cost_micro: int = 0, cost_priced: bool = True) -> dict[str, Any]:
    return {
        "gen_ai.usage.input_tokens": int(input_tokens or 0),
        "gen_ai.usage.output_tokens": int(output_tokens or 0),
        # 语义约定没有成本字段（价格不是协议的一部分），用项目前缀标明出处
        "meetingbot.cost_micro": int(cost_micro or 0),
        "meetingbot.cost_priced": bool(cost_priced),
    }


__all__ = ["setup_tracing", "enabled", "span", "tracer", "model_attributes",
           "usage_attributes"]
