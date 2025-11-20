"""限流与预算闸：滑动窗口计数，集中式存储（Redis），读不到就放行。

## 为什么必须搬出进程内存

旧实现把计数放在模块级字典里，注释写着"backend 以 --workers 1 运行，语义一致"。
那句话是对的，也是**约束**：多副本部署下每个进程各算一份，限流形同虚设
（8 次失败阈值 × N 个副本）。因此这里有两条路：

- `REDIS_URL` 已配置 → Redis 滑动窗口，计数与判定在**一段 Lua 里原子完成**
  （读-判-写分开做的话，并发请求会同时通过）；
- 未配置 → 退回进程内存实现（本地开发与单测），语义与旧实现一致。

## fail-open 是刻意的

Redis 不可达时**放行**，而不是拒绝。理由：限流保护的是"别被刷爆"，而限流器自己
故障时如果一律拒绝，等于把"计数组件挂了"升级成"整个登录与问答不可用"——可用性
损失远大于被刷的风险。故障会记一条 ERROR 日志，容器有健康检查与告警可查。

## 两种用途，同一套计数

1. **失败计数**（登录/注册防爆破）：记失败、成功时清零、超阈值则拦（`is_blocked`）；
2. **配额**（问答的 token/成本预算、按 IP 的通用限流）：一段时间内累计到一个上限
   就拒（`consume` / `check`），`retry_after` 用于标准 429 响应头。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger("meetingbot.ratelimit")

_LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "900"))
_LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_RATE_MAX_FAILURES", "8"))

# 滑动窗口用有序集合实现：成员是"这次命中的时间戳"，清理时按分数（时间）裁掉
# 窗口外的成员。之所以不用计数器 + 固定窗口：固定窗口在边界上能放进两倍流量。
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now_ms, member)
  redis.call('PEXPIRE', key, window_ms)
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_ms = 0
if oldest[2] then
  retry_ms = window_ms - (now_ms - tonumber(oldest[2]))
  if retry_ms < 0 then retry_ms = 0 end
end
return {0, 0, retry_ms}
"""

# 配额累加：INCRBY 与首次设置过期时间必须在同一段脚本里，
# 否则"先 INCRBY 再 EXPIRE"之间的崩溃会留下永不过期的计数器（配额被永久占满）
_QUOTA_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local window_ms = tonumber(ARGV[3])
local used = tonumber(redis.call('GET', key) or '0')
if limit > 0 and used + amount > limit then
  local ttl = redis.call('PTTL', key)
  if ttl < 0 then ttl = window_ms end
  return {0, used, ttl}
end
local total = redis.call('INCRBY', key, amount)
if redis.call('PTTL', key) < 0 then
  redis.call('PEXPIRE', key, window_ms)
end
return {1, total, window_ms}
"""


@dataclass(frozen=True)
class Decision:
    """一次判定：允许与否、还剩多少、被拒时建议多久后重试。"""

    allowed: bool
    remaining: int
    retry_after_seconds: int
    limit: int

    def headers(self) -> dict[str, str]:
        """标准限流响应头（429 与正常响应都带，客户端才能自适应）。"""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after_seconds))
            headers["X-RateLimit-Reset"] = str(
                int(time.time()) + max(1, self.retry_after_seconds))
        return headers


# --------------------------------------------------------------------------- #
# 存储后端
# --------------------------------------------------------------------------- #


class _MemoryBackend:
    """进程内实现：本地开发与单测用。多副本下不共享计数（见模块文档）。"""

    def __init__(self) -> None:
        # key -> [(时间戳秒, 增量)]
        self._series: dict[str, list[tuple[float, float]]] = {}
        self._totals: dict[str, tuple[float, float, float]] = {}

    def _prune(self, key: str, now: float, window_seconds: float) -> None:
        entries = self._series.get(key)
        if not entries:
            return
        keep = [(at, value) for at, value in entries if now - at < window_seconds]
        if keep:
            self._series[key] = keep
        else:
            self._series.pop(key, None)

    async def sliding(self, key: str, *, limit: int, window_seconds: float,
                      amount: int = 1) -> Decision:
        now = time.monotonic()
        self._prune(key, now, window_seconds)
        entries = self._series.setdefault(key, [])
        used = sum(value for _, value in entries)
        if used + amount > limit:
            oldest = min((at for at, _ in entries), default=now)
            retry = int(window_seconds - (now - oldest)) + 1
            return Decision(False, 0, max(1, retry), limit)
        entries.append((now, amount))
        return Decision(True, int(limit - used - amount), 0, limit)

    async def quota(self, key: str, *, limit: int, window_seconds: float,
                    amount: float) -> Decision:
        now = time.monotonic()
        total, expires_at = self._totals.get(key, (0.0, now + window_seconds))
        if now >= expires_at:
            total, expires_at = 0.0, now + window_seconds
        if limit > 0 and total + amount > limit:
            return Decision(False, 0, max(1, int(expires_at - now)), limit)
        total += amount
        self._totals[key] = (total, expires_at)
        return Decision(True, max(0, int(limit - total)) if limit > 0 else 0, 0, limit)

    async def quota_remaining(self, key: str, *, limit: float,
                              window_seconds: float) -> float:
        total, expires_at = self._totals.get(key, (0.0, 0.0))
        if time.monotonic() >= expires_at:
            return limit
        return max(0.0, limit - total)

    async def count(self, key: str, *, window_seconds: float) -> int:
        now = time.monotonic()
        entries = [(at, value) for at, value in self._series.get(key, []) if now - at < window_seconds]
        return int(sum(value for _, value in entries))

    async def clear(self, key: str) -> None:
        self._series.pop(key, None)

    async def seconds_left(self, key: str, *, window_seconds: float) -> int:
        entries = self._series.get(key) or []
        if not entries:
            return 0
        oldest_at = min(at for at, _ in entries)
        return max(1, int(window_seconds - (time.monotonic() - oldest_at)))


class _RedisBackend:
    """Redis 实现：判定与写入在一段 Lua 里完成（原子）。所有错误一律 fail-open。"""

    def __init__(self, client) -> None:  # noqa: ANN001 - redis.asyncio.Redis
        self._client = client

    async def _eval(self, script: str, keys: list[str], args: list) -> list | None:
        try:
            return await self._client.eval(script, len(keys), *keys, *args)
        except Exception:  # noqa: BLE001 - 计数组件故障不该让业务不可用
            logger.exception("Redis 限流判定失败，本次按放行处理（fail-open）")
            return None

    async def sliding(self, key: str, *, limit: int, window_seconds: float,
                      amount: int = 1) -> Decision:
        member = f"{time.time() * 1000:.0f}-{os.urandom(4).hex()}"
        result = await self._eval(_SLIDING_WINDOW_LUA, [key], [
            int(time.time() * 1000), int(window_seconds * 1000), limit, member])
        if result is None:
            return Decision(True, limit, 0, limit)
        allowed, remaining, retry_ms = int(result[0]), int(result[1]), int(result[2])
        return Decision(bool(allowed), remaining, max(1, retry_ms // 1000) if not allowed else 0, limit)

    async def quota(self, key: str, *, limit: int, window_seconds: float,
                    amount: float) -> Decision:
        result = await self._eval(_QUOTA_LUA, [key], [
            int(amount), limit, int(window_seconds * 1000)])
        if result is None:
            return Decision(True, 0, 0, limit)
        allowed, used, ttl_ms = int(result[0]), int(result[1]), int(result[2])
        remaining = max(0, limit - used) if limit > 0 else 0
        return Decision(bool(allowed), remaining,
                        max(1, ttl_ms // 1000) if not allowed else 0, limit)

    async def quota_remaining(self, key: str, *, limit: float,
                              window_seconds: float) -> float:
        try:
            value = await self._client.get(key)
        except Exception:  # noqa: BLE001 - fail-open
            return limit
        return max(0.0, limit - float(value or 0))

    async def count(self, key: str, *, window_seconds: float) -> int:
        try:
            entries = await self._client.zrangebyscore(
                key, time.time() * 1000 - window_seconds * 1000, '+inf')
        except Exception:  # noqa: BLE001 - 读不到计数就按未超限处理（fail-open）
            logger.exception("读取窗口计数失败，按 0 处理（fail-open）")
            return 0
        return len(entries)

    async def clear(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception:  # noqa: BLE001 - fail-open
            logger.exception("清除计数失败（fail-open）")

    async def seconds_left(self, key: str, *, window_seconds: float) -> int:
        window_ms = window_seconds * 1000
        try:
            oldest = await self._client.zrange(key, 0, 0, withscores=True)
        except Exception:  # noqa: BLE001 - fail-open
            return 0
        if not oldest:
            return 0
        elapsed_ms = time.time() * 1000 - oldest[0][1]
        return max(1, int((window_ms - elapsed_ms) / 1000))


_backend: object | None = None


def _redis_client():  # noqa: ANN202 - 返回类型依赖可选依赖
    from redis.asyncio import Redis  # 延迟导入：没装 redis 也能跑（退回内存实现）
    return Redis.from_url(os.getenv("REDIS_URL", ""), decode_responses=True)


def backend():  # noqa: ANN202
    """当前后端（进程级单例）。REDIS_URL 有配置就用 Redis，否则用内存实现。"""
    global _backend
    if _backend is None:
        url = os.getenv("REDIS_URL", "").strip()
        if url:
            try:
                _backend = _RedisBackend(_redis_client())
            except Exception:  # noqa: BLE001 - redis 包缺失或 URL 非法：退回内存并留日志
                logger.exception("Redis 后端初始化失败，退回进程内限流（%s）", url)
                _backend = _MemoryBackend()
        else:
            _backend = _MemoryBackend()
    return _backend


def reset_backend() -> None:
    """测试用：丢掉单例，让下一次调用重新按环境变量选后端。"""
    global _backend
    _backend = None


def backend_name() -> str:
    return "redis" if isinstance(backend(), _RedisBackend) else "memory"


# --------------------------------------------------------------------------- #
# 用途一：失败计数（登录/注册防爆破）
# --------------------------------------------------------------------------- #


async def is_blocked(kind: str, key: str) -> bool:
    failures = await backend().count(f"rl:{kind}:{key}", window_seconds=_LOGIN_WINDOW_SECONDS)
    return failures >= _LOGIN_MAX_FAILURES


async def record_failure(kind: str, key: str) -> None:
    # 失败计数不设上限：超出阈值本身要被看见（is_blocked 读的就是这个计数）
    await backend().sliding(f"rl:{kind}:{key}", limit=10_000,
                            window_seconds=_LOGIN_WINDOW_SECONDS, amount=1)


async def record_success(kind: str, key: str) -> None:
    await backend().clear(f"rl:{kind}:{key}")


async def seconds_left(kind: str, key: str) -> int:
    """还要等多久。滑动窗口下等于"最老一条失败记录出窗"的剩余时间。"""
    return await backend().seconds_left(f"rl:{kind}:{key}", window_seconds=_LOGIN_WINDOW_SECONDS)


# --------------------------------------------------------------------------- #
# 用途二：配额（通用限流 + 问答预算）
# --------------------------------------------------------------------------- #


async def check(kind: str, key: str, *, limit: int, window_seconds: int) -> Decision:
    """这一请求是否允许（允许即计入一次）。"""
    if limit <= 0:
        return Decision(True, 0, 0, 0)
    return await backend().sliding(f"rl:{kind}:{key}", limit=limit,
                                   window_seconds=window_seconds, amount=1)


async def spend(kind: str, key: str, amount: float, *, limit: float,
                window_seconds: int) -> Decision:
    """记一笔配额消耗（问答的 token / 成本预算）。`limit<=0` 表示不设限。"""
    if limit <= 0:
        return Decision(True, 0, 0, 0)
    return await backend().quota(f"quota:{kind}:{key}", limit=limit,
                                 window_seconds=window_seconds, amount=amount)


async def quota_remaining(kind: str, key: str, *, limit: float,
                          window_seconds: int) -> float:
    """只读：当前窗口还剩多少配额（不写入）。"""
    if limit <= 0:
        return 0
    return await backend().quota_remaining(f"quota:{kind}:{key}", limit=limit,
                                           window_seconds=window_seconds)


async def healthy() -> bool:
    """限流后端是否可用（供 /health 汇总；不支持的后端返回 True）。"""
    store = backend()
    if not isinstance(store, _RedisBackend):
        return True
    try:
        return bool(await store._client.ping())
    except Exception:  # noqa: BLE001
        return False
