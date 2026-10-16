"""结构化日志与指标：标准库 JSON 日志 + Prometheus 文本。

## 日志为什么用标准库自己拼

结构化日志的库（structlog 之类）能省几行代码，代价是多一个依赖、多一套需要跟版本
的 API，而这里要的只是"每条日志一行 JSON"。`logging.Formatter` 子类就够了，
且与 uvicorn、tortoise 这些已经用标准库 logging 的组件天然一致。

## 指标名与 OTel 语义约定的对应

指标名沿用 OTel GenAI 语义约定（`gen_ai.client.token.usage` 等），但 Prometheus 的
指标名不允许出现点号，所以对外暴露时点号换成下划线、计数器加 `_total` 后缀：

| OTel 语义约定 | /metrics 里的名字 |
|---|---|
| `gen_ai.client.token.usage` | `gen_ai_client_token_usage_total{type="input"\\|"output"}` |
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_seconds` |
| 成本（本项目自定义） | `meetingbot_ai_call_cost_micro_total` |

两套名字都写在这里与 `docs/架构.md` 里，避免"面板上的名字和语义约定对不上"。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

# --------------------------------------------------------------------------- #
# JSON 日志
# --------------------------------------------------------------------------- #


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。异常堆栈进 `exception` 字段，不会把多行内容糊在一起。"""

    # 标准字段之外的附加字段（extra=...）统一放进 `extra`，避免与保留字冲突
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = {key: value for key, value in record.__dict__.items()
                 if key not in self._RESERVED and not key.startswith("_")}
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """装一次全局日志格式。`LOG_FORMAT=text` 可退回人类可读格式（本地开发用）。"""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    use_json = os.getenv("LOG_FORMAT", "json").strip().lower() != "text"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn 默认自带 handler（会与上面这份重复打印）；清掉，让它走同一条管道
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


# --------------------------------------------------------------------------- #
# 指标（Prometheus 文本，供 /metrics 抓取）
# --------------------------------------------------------------------------- #

# 独立 registry：不用全局默认的，测试之间不会互相污染（也就不会出现
# "同一个 Counter 注册两次"的报错）
REGISTRY = CollectorRegistry()

TOKEN_COUNTER = Counter(
    "gen_ai_client_token_usage_total",
    "模型调用的 token 用量（按输入/输出分开，对应 OTel gen_ai.client.token.usage）",
    ["model", "call_type", "type"],
    registry=REGISTRY,
)
DURATION_HISTOGRAM = Histogram(
    "gen_ai_client_operation_duration_seconds",
    "模型调用耗时（对应 OTel gen_ai.client.operation.duration）",
    ["model", "call_type", "status"],
    # 分桶按本项目的真实量级取：机械抽取在 1s 上下，判定类与长输入在 10s 以上
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
COST_COUNTER = Counter(
    "meetingbot_ai_call_cost_micro_total",
    "模型调用成本（微元，1e-6 元；只统计已定价的调用）",
    ["model", "call_type"],
    registry=REGISTRY,
)
UNPRICED_COUNTER = Counter(
    "meetingbot_ai_call_unpriced_total",
    "未定价的模型调用次数（成本为 0 不等于免费，这就是那个差额）",
    ["model", "call_type"],
    registry=REGISTRY,
)


def record_model_call(*, model: str, call_type: str, status: str, elapsed_ms: int,
                      input_tokens: int = 0, output_tokens: int = 0,
                      cost_micro: int = 0, priced: bool = True) -> None:
    """记一次模型调用。**失败也要记**（成功率是这三个指标之外最常被问的问题）。"""
    label_model = model or "unknown"
    label_type = call_type or "unknown"
    DURATION_HISTOGRAM.labels(model=label_model, call_type=label_type,
                              status=status).observe(max(0, elapsed_ms) / 1000)
    if status == "SUCCEEDED":
        if input_tokens:
            TOKEN_COUNTER.labels(model=label_model, call_type=label_type,
                                 type="input").inc(input_tokens)
        if output_tokens:
            TOKEN_COUNTER.labels(model=label_model, call_type=label_type,
                                 type="output").inc(output_tokens)
        if priced:
            COST_COUNTER.labels(model=label_model, call_type=label_type).inc(cost_micro)
        else:
            UNPRICED_COUNTER.labels(model=label_model, call_type=label_type).inc()


def render() -> bytes:
    """Prometheus 文本格式，供 `/metrics` 直接返回。"""
    return generate_latest(REGISTRY)


__all__ = ["JsonFormatter", "setup_logging", "record_model_call", "render", "REGISTRY"]
