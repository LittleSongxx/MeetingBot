"""限流与配额的单测：零 Redis、零网络。

钉住四组行为（它们出错都不会报错，只会"限流失效"或"业务不可用"）：
- 滑动窗口在窗口内累计、超限拒绝、到期后重新放行；
- 配额（token/成本预算）只累加、超限拒绝、窗口到期归零；
- Redis 故障时 **fail-open**（放行并记日志），而不是把业务一起拒掉；
- 两个后端（内存/Redis）行为一致——测试用同一个假客户端验证 Redis 分支的 Lua 调用形状。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from common import ratelimit


class MemoryBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ratelimit.reset_backend()
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            ratelimit.backend()

    async def asyncTearDown(self):
        ratelimit.reset_backend()

    async def test_sliding_window_allows_then_blocks(self):
        decisions = [await ratelimit.check("test", "k1", limit=3, window_seconds=60)
                     for _ in range(4)]
        self.assertEqual([d.allowed for d in decisions], [True, True, True, False])
        self.assertEqual(decisions[0].remaining, 2)
        self.assertGreaterEqual(decisions[3].retry_after_seconds, 1)

    async def test_headers_are_standard(self):
        decision = await ratelimit.check("test", "k2", limit=1, window_seconds=60)
        headers = decision.headers()
        self.assertEqual(headers["X-RateLimit-Limit"], "1")
        self.assertEqual(headers["X-RateLimit-Remaining"], "0")
        blocked = await ratelimit.check("test", "k2", limit=1, window_seconds=60)
        self.assertIn("Retry-After", blocked.headers())
        self.assertIn("X-RateLimit-Reset", blocked.headers())

    async def test_window_expiry_releases_the_key(self):
        clock = {"now": 100.0}
        with patch("time.monotonic", side_effect=lambda: clock["now"]):
            await ratelimit.check("test", "k3", limit=1, window_seconds=60)
            blocked = await ratelimit.check("test", "k3", limit=1, window_seconds=60)
            self.assertFalse(blocked.allowed)
            # 窗口过去之后同键重新放行
            clock["now"] = 200.0
            later = await ratelimit.check("test", "k3", limit=1, window_seconds=60)
            self.assertTrue(later.allowed)

    async def test_limit_zero_means_unlimited(self):
        for _ in range(5):
            decision = await ratelimit.check("test", "k4", limit=0, window_seconds=60)
            self.assertTrue(decision.allowed)

    async def test_quota_accumulates_until_the_limit(self):
        first = await ratelimit.spend("chat", "u1", 40, limit=100, window_seconds=3600)
        self.assertTrue(first.allowed)
        second = await ratelimit.spend("chat", "u1", 50, limit=100, window_seconds=3600)
        self.assertTrue(second.allowed)
        # 边界是"含"的：正好用满再花 1 才被拒（这样"用完最后一分预算"不会被误伤）
        boundary = await ratelimit.spend("chat", "u1", 10, limit=100, window_seconds=3600)
        self.assertTrue(boundary.allowed)
        over = await ratelimit.spend("chat", "u1", 1, limit=100, window_seconds=3600)
        self.assertFalse(over.allowed)
        self.assertGreaterEqual(over.retry_after_seconds, 1)

    async def test_quota_remaining_is_read_only(self):
        await ratelimit.spend("chat", "u2", 30, limit=100, window_seconds=3600)
        self.assertEqual(await ratelimit.quota_remaining("chat", "u2", limit=100,
                                                         window_seconds=3600), 70)
        self.assertEqual(await ratelimit.quota_remaining("chat", "u2", limit=100,
                                                         window_seconds=3600), 70)

    async def test_failures_block_then_success_clears(self):
        self.assertFalse(await ratelimit.is_blocked("login-user", "alice"))
        for _ in range(ratelimit._LOGIN_MAX_FAILURES):
            await ratelimit.record_failure("login-user", "alice")
        self.assertTrue(await ratelimit.is_blocked("login-user", "alice"))
        self.assertGreater(await ratelimit.seconds_left("login-user", "alice"), 0)
        await ratelimit.record_success("login-user", "alice")
        self.assertFalse(await ratelimit.is_blocked("login-user", "alice"))

    async def test_backend_name_reports_which_store_is_in_use(self):
        self.assertEqual(ratelimit.backend_name(), "memory")


class _FakeRedisError(RuntimeError):
    pass


class _FakeRedis:
    """Redis 客户端的替身：只实现 Lua 脚本与几条命令，用真实语义模拟。

    它不解释 Lua，而是按脚本里的关键字选择内存实现的行为——这样测的是
    "我们发了什么脚本、怎么解读返回值"，而不是"Lua 写得对不对"
    （后者只能靠真容器验，见验收命令里的 docker run）。
    """

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, tuple]] = []
        self.windows: dict[str, list[float]] = {}
        self.counters: dict[str, float] = {}

    async def eval(self, script: str, numkeys: int, *args):
        self.calls.append((script, args))
        if self.fail:
            raise _FakeRedisError("connection refused")
        if "ZREMRANGEBYSCORE" in script:
            key, now_ms, window_ms, limit, _member = args
            entries = [at for at in self.windows.get(key, []) if now_ms - at < window_ms]
            if len(entries) < limit:
                entries.append(now_ms)
                self.windows[key] = entries
                return [1, limit - len(entries), 0]
            self.windows[key] = entries
            oldest = min(entries) if entries else now_ms
            return [0, 0, max(0, window_ms - (now_ms - oldest))]
        if "INCRBY" in script:
            key, amount, limit, _window_ms = args
            used = self.counters.get(key, 0)
            if limit > 0 and used + amount > limit:
                return [0, used, 1000]
            self.counters[key] = used + amount
            return [1, self.counters[key], 1000]
        raise AssertionError("未知脚本")

    async def zrangebyscore(self, key, _min, _max):
        return self.windows.get(key, [])

    async def zrange(self, key, _start, _stop, **_kwargs):
        entries = sorted(self.windows.get(key, []))
        return [(f"m{index}", at) for index, at in enumerate(entries)]

    async def delete(self, key):
        self.windows.pop(key, None)
        self.counters.pop(key, None)

    async def get(self, key):
        return self.counters.get(key)

    async def ping(self):
        if self.fail:
            raise _FakeRedisError("connection refused")
        return True


class RedisBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ratelimit.reset_backend()
        self.fake = _FakeRedis()
        with patch.dict(os.environ, {"REDIS_URL": "redis://fake/0"}), \
                patch.object(ratelimit, "_redis_client", return_value=self.fake):
            ratelimit.backend()

    async def asyncTearDown(self):
        ratelimit.reset_backend()

    async def test_uses_redis_when_configured(self):
        self.assertEqual(ratelimit.backend_name(), "redis")

    async def test_decision_shape_matches_the_memory_backend(self):
        allowed = await ratelimit.check("api", "u1", limit=2, window_seconds=60)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.remaining, 1)
        await ratelimit.check("api", "u1", limit=2, window_seconds=60)
        blocked = await ratelimit.check("api", "u1", limit=2, window_seconds=60)
        self.assertFalse(blocked.allowed)
        self.assertGreaterEqual(blocked.retry_after_seconds, 1)

    async def test_quota_goes_through_the_atomic_script(self):
        await ratelimit.spend("chat", "u2", 60, limit=100, window_seconds=3600)
        blocked = await ratelimit.spend("chat", "u2", 60, limit=100, window_seconds=3600)
        self.assertFalse(blocked.allowed)
        self.assertEqual(await ratelimit.quota_remaining("chat", "u2", limit=100,
                                                         window_seconds=3600), 40)

    async def test_redis_failure_is_fail_open(self):
        self.fake.fail = True
        # 判定失败时放行（可用性优先），但会留下 ERROR 日志
        with patch.object(ratelimit.logger, "exception") as logged:
            decision = await ratelimit.check("api", "u3", limit=1, window_seconds=60)
            self.assertTrue(decision.allowed)
            self.assertEqual(await ratelimit.quota_remaining("chat", "u3", limit=10,
                                                             window_seconds=60), 10)
        self.assertTrue(logged.called)

    async def test_health_reports_redis_state(self):
        self.assertTrue(await ratelimit.healthy())
        self.fake.fail = True
        self.assertFalse(await ratelimit.healthy())

    async def test_login_failure_counting_uses_redis_windows(self):
        await ratelimit.record_failure("login-user", "bob")
        self.assertFalse(await ratelimit.is_blocked("login-user", "bob"))
        await ratelimit.record_success("login-user", "bob")
        self.assertEqual(await ratelimit.seconds_left("login-user", "bob"), 0)


class RedisScriptShapeTest(unittest.IsolatedAsyncioTestCase):
    """脚本必须是原子的：判定与写入在**同一次** eval 里完成。"""

    async def test_sliding_window_is_one_eval_call(self):
        ratelimit.reset_backend()
        fake = _FakeRedis()
        with patch.dict(os.environ, {"REDIS_URL": "redis://fake/0"}), \
                patch.object(ratelimit, "_redis_client", return_value=fake):
            ratelimit.backend()
        await ratelimit.check("api", "u9", limit=5, window_seconds=60)
        self.assertEqual(len(fake.calls), 1, "判定与计数必须是同一次原子调用")
        script, args = fake.calls[0]
        self.assertIn("ZREMRANGEBYSCORE", script)
        self.assertIn("ZADD", script, "判定与计数必须是同一次原子调用")
        self.assertEqual(args[0], "rl:api:u9")
        ratelimit.reset_backend()


if __name__ == "__main__":
    unittest.main()
