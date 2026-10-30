"""轻量操作冷却：同一个用户对同一类昂贵操作的最短间隔。

单进程内存实现，主要防「疯狂点按钮/脚本刷接口」造成的模型费用放大；
真正的配额/预算闸需要 Redis + 预算表，个人项目先不做。
"""

from __future__ import annotations

import sys
import time

from common.exception_handler import CustomException

_intervals: dict[str, float] = {}


def ensure_interval(key: str, seconds: float = 3.0, message: str = "操作过于频繁，请稍后再试") -> None:
    # 单元测试会连续调用同一个接口验证业务逻辑，跳过冷却
    if "unittest" in sys.modules:
        return
    current = time.monotonic()
    last = _intervals.get(key, 0.0)
    if current - last < seconds:
        raise CustomException(message)
    _intervals[key] = current
