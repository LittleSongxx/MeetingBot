"""登录失败限流：单进程内存实现，避免同一账号/来源被无限次试密码。

单进程部署（本项目强制 --workers 1）下足够；将来多实例时需要换成 Redis 计数。
"""

from __future__ import annotations

import time

MAX_ATTEMPTS = 5
LOCK_SECONDS = 300

# key -> 失败次数；key = 账号|来源IP
_failures: dict[str, int] = {}
# key -> 解锁时间戳
_locked_until: dict[str, float] = {}


def _key(username: str | None, client_host: str | None) -> str:
    return f"{(username or '').strip().lower()}|{client_host or '-'}"


def check_locked(username: str | None, client_host: str | None) -> int:
    """返回剩余锁定秒数；0 表示未锁定。"""
    key = _key(username, client_host)
    until = _locked_until.get(key, 0)
    remaining = int(until - time.time())
    if remaining <= 0:
        _locked_until.pop(key, None)
        return 0
    return remaining


def record_failure(username: str | None, client_host: str | None) -> None:
    key = _key(username, client_host)
    _failures[key] = _failures.get(key, 0) + 1
    if _failures[key] >= MAX_ATTEMPTS:
        _locked_until[key] = time.time() + LOCK_SECONDS
        _failures[key] = 0


def clear(username: str | None, client_host: str | None) -> None:
    key = _key(username, client_host)
    _failures.pop(key, None)
    _locked_until.pop(key, None)
