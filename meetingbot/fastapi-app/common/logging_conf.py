"""日志格式：标准库 logging + 一行一条 JSON。

结构化日志的库（structlog 之类）能省几行代码，代价是多一个依赖、多一套需要跟版本
的 API；这里要的只是"每条日志一行 JSON"，`logging.Formatter` 子类就够了，
且与 uvicorn、tortoise 这些已经用标准库 logging 的组件天然一致。

`LOG_FORMAT=text` 退回人类可读格式（本地开发用），`LOG_LEVEL` 控制级别。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


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
    """装一次全局日志格式；重复调用保持幂等。"""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    use_json = os.getenv("LOG_FORMAT", "json").strip().lower() != "text"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if use_json else logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn 默认自带 handler（会与上面这份重复打印）；清掉，让它走同一条管道
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
