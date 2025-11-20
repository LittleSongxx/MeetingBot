"""登录/注册的进程内限流。

防在线爆破的最小实现：滑动窗口失败计数，按 用户名 与 来源IP 两个键独立
累计，任一键达到阈值即拒绝。进程内字典（单 worker 部署语义与
_running_tasks 等内存态结构一致）；多副本部署需要换集中式存储（见
DEPLOYMENT.md 的单进程约束）。
"""

from __future__ import annotations

import os
import time

_LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "900"))
_LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_RATE_MAX_FAILURES", "8"))

# (kind, key) -> [window_start, failures]
_counters: dict[tuple[str, str], list] = {}


def _prune(now: float) -> None:
    stale = [k for k, v in _counters.items() if now - v[0] >= _LOGIN_WINDOW_SECONDS]
    for key in stale:
        _counters.pop(key, None)


def is_blocked(kind: str, key: str) -> bool:
    now = time.monotonic()
    _prune(now)
    entry = _counters.get((kind, key))
    return bool(entry) and entry[1] >= _LOGIN_MAX_FAILURES


def record_failure(kind: str, key: str) -> None:
    now = time.monotonic()
    _prune(now)
    entry = _counters.setdefault((kind, key), [now, 0])
    # 窗口过期后重新起算
    if now - entry[0] >= _LOGIN_WINDOW_SECONDS:
        entry[0], entry[1] = now, 0
    entry[1] += 1


def record_success(kind: str, key: str) -> None:
    _counters.pop((kind, key), None)


def seconds_left(kind: str, key: str) -> int:
    entry = _counters.get((kind, key))
    if not entry:
        return 0
    return max(1, int(_LOGIN_WINDOW_SECONDS - (time.monotonic() - entry[0])))
