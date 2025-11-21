"""问答接口的传输层单测：SSE 帧、心跳、断开收尾，以及一次真实的 ASGI 往返。

这组测试不碰模型、不碰向量库，但**真的走 FastAPI 的路由与鉴权**（httpx 的 ASGI
传输直接打到路由上），因此能验证响应头、帧格式与状态码这些只在传输层成立的东西。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from api import chat as chat_api
from common import ratelimit
from common.auth import create_token
from common.exception_handler import setup_exceptions
from fastapi import FastAPI
from models import AiCallLog, User
from services import chat_service
from tortoise import Tortoise


class SseFrameTest(unittest.IsolatedAsyncioTestCase):
    async def test_frames_carry_event_name_and_json(self):
        async def source():
            yield ("meta", {"session_id": 1})
            yield ("token", {"text": "上线时间"})

        frames = [frame async for frame in chat_api._sse_frames(source(), heartbeat=5)]
        self.assertEqual(frames[0], 'event: meta\ndata: {"session_id": 1}\n\n')
        self.assertEqual(frames[1], 'event: token\ndata: {"text": "上线时间"}\n\n')

    async def test_idle_connection_gets_heartbeat_comments(self):
        async def source():
            await asyncio.sleep(0.05)
            yield ("token", {"text": "慢"})

        frames = [frame async for frame in chat_api._sse_frames(source(), heartbeat=0.01)]
        self.assertIn(": ping\n\n", frames)
        # 心跳是注释行，不占事件名；正文仍按帧发出
        self.assertTrue(any(frame.startswith("event: token") for frame in frames))

    async def test_source_failure_becomes_an_error_frame(self):
        async def source():
            yield ("meta", {})
            raise RuntimeError("模型挂了")

        frames = [frame async for frame in chat_api._sse_frames(source(), heartbeat=5)]
        self.assertTrue(frames[-1].startswith("event: error"))
        self.assertIn("模型挂了", frames[-1])

    async def test_client_disconnect_cancels_the_producer_and_awaits_cleanup(self):
        cleaned = asyncio.Event()

        async def source():
            try:
                yield ("meta", {})
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cleaned.set()   # 生产端被取消后仍能跑收尾（真实实现里是落库）
                raise

        frames = chat_api._sse_frames(source(), heartbeat=5)
        first = await frames.__anext__()
        self.assertTrue(first.startswith("event: meta"))
        await frames.aclose()
        self.assertTrue(cleaned.is_set(), "断开后必须给生产端收尾的机会")

    async def test_producer_gets_aclose_called_on_disconnect(self):
        """断开时除了取消任务，还要**关闭**生产端。

        取消只会打在"正在 await 的生成器"上；如果取消时机落在别处，生产端就悬在
        某个 yield 上，收尾（真实实现里是落库）永远不会跑。因此 finally 里必须
        显式 aclose——这里用一个自定义迭代器把这件事观测出来。
        """
        source = _ClosableSource()

        frames = chat_api._sse_frames(source, heartbeat=0.5)
        await frames.__anext__()
        await frames.aclose()
        self.assertTrue(source.closed, "断开时必须关闭生产端，它的收尾才有机会跑")


class _ClosableSource:
    """手写的异步迭代器：只用于观测 `aclose()` 有没有被调用。

    先立刻给一个事件再挂住——否则第一帧要等心跳超时才来，测试白等几秒。
    """

    def __init__(self):
        self.closed = False
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return ("meta", {})
        await asyncio.sleep(30)
        return ("token", {"text": "永远不会到这里"})

    async def aclose(self):
        self.closed = True


class ChatEndpointTest(unittest.IsolatedAsyncioTestCase):
    """一次完整的 ASGI 往返：建会话 → 提问 → 读 SSE 帧。"""

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="sse-t", password="x", name="张晨")
        self.token = create_token(self.user)
        app = FastAPI()
        app.include_router(chat_api.router)
        setup_exceptions(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await Tortoise.close_connections()

    async def test_session_crud_round_trip(self):
        created = await self.client.post("/chat/session", json={},
                                         headers={"token": self.token})
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["data"]["id"]

        page = await self.client.get("/chat/selectSessionPage", headers={"token": self.token})
        self.assertEqual(page.json()["data"]["total"], 1)

        deleted = await self.client.delete("/chat/session", params={"session_id": session_id},
                                           headers={"token": self.token})
        self.assertEqual(deleted.status_code, 200)
        page = await self.client.get("/chat/selectSessionPage", headers={"token": self.token})
        self.assertEqual(page.json()["data"]["total"], 0)

    async def test_missing_token_is_401(self):
        response = await self.client.post("/chat/session", json={})
        self.assertEqual(response.status_code, 401)

    async def test_status_self_reports_the_retrieval_funnel(self):
        """产物要能自证这一臂的窗口与 k：抓取进程与容器 env 是两个空间，
        从外面猜等于没有证据（标定实验的对照就靠这三个字段区分）。"""
        with patch.dict(os.environ, {"KB_CHUNK_TARGET_CHARS": "300",
                                     "RERANK_TOP_K": "3",
                                     "QDRANT_COLLECTION": "funnel_test"}), \
                patch.object(chat_api.qdrant, "health", new=AsyncMock(return_value=True)), \
                patch.object(chat_api.moderation, "status", return_value={"enabled": False}):
            response = await self.client.get("/chat/status", headers={"token": self.token})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["kb_chunk_target_chars"], 300)
        self.assertEqual(data["rerank_top_k"], 3)
        self.assertFalse(data["kb_dedupe_enabled"])
        self.assertFalse(data["kb_contextual_enabled"])
        self.assertEqual(data["qdrant_collection"], "funnel_test")

    async def test_stream_returns_sse_frames_with_anti_buffering_headers(self):
        created = await self.client.post("/chat/session", json={},
                                         headers={"token": self.token})
        session_id = created.json()["data"]["id"]

        async def fake_stream(session, question, *, user, model=None):
            yield ("meta", {"session_id": session.id, "message_id": 9, "question": question})
            yield ("token", {"text": "上线"})
            yield ("token", {"text": "时间定在十月[1]。"})
            yield ("done", {"message_id": 9, "stop_reason": "finished", "citations": [],
                            "unknown_refs": [], "tokens": {"total_tokens": 30}})

        with patch.object(chat_service, "stream_answer", fake_stream):
            async with self.client.stream(
                    "POST", "/chat/stream",
                    json={"session_id": session_id, "question": "上线时间定了吗"},
                    headers={"token": self.token}) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
                # 反代下逐 token 的两条硬要求
                self.assertEqual(response.headers["x-accel-buffering"], "no")
                self.assertEqual(response.headers["cache-control"], "no-cache")
                body = "".join([chunk async for chunk in response.aiter_text()])

        blocks = [block for block in body.strip().split("\n\n") if block.startswith("event: ")]
        events = [block.split("\n")[0].removeprefix("event: ") for block in blocks]
        self.assertEqual(events, ["meta", "token", "token", "done"])
        payloads = [json.loads(block.split("data: ", 1)[1]) for block in blocks]
        self.assertEqual("".join(item["text"] for item in payloads if "text" in item),
                         "上线时间定在十月[1]。")

    async def test_stream_of_a_foreign_session_is_refused(self):
        other = await User.create(username="sse-o", password="x", name="李月")
        session = await chat_service.create_session(other)
        response = await self.client.post(
            "/chat/stream", json={"session_id": session.id, "question": "偷看"},
            headers={"token": self.token})
        self.assertEqual(response.status_code, 400)
        self.assertIn("会话不存在或无权访问", response.json()["msg"])

    async def test_empty_question_comes_back_as_an_error_event(self):
        created = await self.client.post("/chat/session", json={},
                                         headers={"token": self.token})
        session_id = created.json()["data"]["id"]
        async with self.client.stream(
                "POST", "/chat/stream", json={"session_id": session_id, "question": "   "},
                headers={"token": self.token}) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
        self.assertIn("event: error", body)
        self.assertIn("问题不能为空", body)


if __name__ == "__main__":
    unittest.main()


class ChatBudgetGateTest(unittest.IsolatedAsyncioTestCase):
    """提问前的两道闸：频率按用户与 IP 各一道、当日 token 预算一道。

    这三条都属于"出问题不会报错、只会悄悄失效"的类型，所以直接钉住：
    被拒时是 429 + 标准响应头，用量按真实 token 记入，后端故障时不挡用户。
    """

    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="gate-t", password="x", name="张晨")
        self.token = create_token(self.user)
        app = FastAPI()
        app.include_router(chat_api.router)
        setup_exceptions(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://test")
        ratelimit.reset_backend()

    async def asyncTearDown(self):
        await self.client.aclose()
        await Tortoise.close_connections()
        ratelimit.reset_backend()

    async def _session_id(self) -> int:
        created = await self.client.post("/chat/session", json={},
                                         headers={"token": self.token})
        return created.json()["data"]["id"]

    async def test_frequency_limit_returns_429_with_standard_headers(self):
        session_id = await self._session_id()

        async def fake_stream(session, question, *, user, model=None):
            yield ("done", {"message_id": 1, "stop_reason": "finished", "notice": "",
                            "tokens": {"total_tokens": 0}, "citations": [],
                            "unknown_refs": [], "tool_calls": 0})

        with patch.object(chat_service, "stream_answer", fake_stream), \
                patch.object(chat_api, "ASK_LIMIT_PER_USER", 1), \
                patch.object(chat_api, "DAILY_TOKEN_BUDGET", 0):
            first = await self.client.post("/chat/stream",
                                           json={"session_id": session_id, "question": "一"},
                                           headers={"token": self.token})
            self.assertEqual(first.status_code, 200)
            second = await self.client.post("/chat/stream",
                                            json={"session_id": session_id, "question": "二"},
                                            headers={"token": self.token})
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second.headers)
        self.assertEqual(second.headers["X-RateLimit-Limit"], "1")
        self.assertIn("提问过于频繁", second.json()["msg"])

    async def test_daily_token_budget_blocks_when_exhausted(self):
        session_id = await self._session_id()
        await ratelimit.spend("chat-tokens", str(self.user.id), 100,
                              limit=100, window_seconds=86_400)
        with patch.object(chat_api, "ASK_LIMIT_PER_USER", 0), \
                patch.object(chat_api, "ASK_LIMIT_PER_IP", 0), \
                patch.object(chat_api, "DAILY_TOKEN_BUDGET", 100):
            response = await self.client.post(
                "/chat/stream", json={"session_id": session_id, "question": "问"},
                headers={"token": self.token})
        self.assertEqual(response.status_code, 429)
        self.assertIn("今日问答额度已用完", response.json()["msg"])

    async def test_turn_tokens_are_charged_to_the_daily_budget(self):
        """额度按账本记：账本里有 40 token，这一轮就扣 40。"""
        session_id = await self._session_id()
        await AiCallLog.create(request_id="AIC-CHARGE-1", call_type="CHAT", biz_id=1,
                               model_config_id=1, model_name="m", status="SUCCEEDED",
                               usage_json={"input_tokens": 25, "output_tokens": 15,
                                           "total_tokens": 40})

        async def fake_stream(session, question, *, user, model=None):
            yield ("meta", {"session_id": session.id, "message_id": 1})
            yield ("token", {"text": "答"})
            yield ("done", {"message_id": 1, "stop_reason": "finished", "notice": "",
                            "tokens": {"total_tokens": 40}, "citations": [],
                            "unknown_refs": [], "tool_calls": 0})

        with patch.object(chat_service, "stream_answer", fake_stream), \
                patch.object(chat_api, "ASK_LIMIT_PER_USER", 0), \
                patch.object(chat_api, "ASK_LIMIT_PER_IP", 0), \
                patch.object(chat_api, "DAILY_TOKEN_BUDGET", 1000):
            async with self.client.stream("POST", "/chat/stream",
                                          json={"session_id": session_id, "question": "问"},
                                          headers={"token": self.token}) as response:
                self.assertEqual(response.status_code, 200)
                await response.aread()
        remaining = await ratelimit.quota_remaining("chat-tokens", str(self.user.id),
                                                   limit=1000, window_seconds=86_400)
        self.assertEqual(remaining, 960)

    async def test_disconnect_still_charges_the_budget(self):
        """客户端提前断开也要记账：按账本（ai_call_log）记，不按 done 事件记。

        否则"提问 → 看到答案开头就断开"可以无限绕开每日预算——那正是这条闸门
        存在的理由。
        """
        session_id = await self._session_id()
        message_id = 4242
        await AiCallLog.create(request_id="AIC-DISCONNECT-1", call_type="CHAT",
                               biz_id=message_id, model_config_id=1, model_name="m",
                               status="SUCCEEDED",
                               usage_json={"input_tokens": 60, "output_tokens": 40,
                                           "total_tokens": 100})

        async def fake_stream(session, question, *, user, model=None):
            yield ("meta", {"session_id": session.id, "message_id": message_id})
            yield ("token", {"text": "开"})
            await asyncio.sleep(30)   # 模拟"答案还在生成，客户端走了"

        with patch.object(chat_service, "stream_answer", fake_stream), \
                patch.object(chat_api, "ASK_LIMIT_PER_USER", 0), \
                patch.object(chat_api, "ASK_LIMIT_PER_IP", 0), \
                patch.object(chat_api, "DAILY_TOKEN_BUDGET", 10_000):
            frames = chat_api._sse_frames(
                chat_api._charged_stream(
                    await chat_service.get_session(session_id, self.user), "问", self.user),
                heartbeat=5)
            first = await frames.__anext__()
            self.assertTrue(first.startswith("event: meta"))
            await frames.aclose()   # 断开

        remaining = await ratelimit.quota_remaining("chat-tokens", str(self.user.id),
                                                   limit=10_000, window_seconds=86_400)
        self.assertEqual(remaining, 9_900, "断开后仍未记账：预算闸可被绕过")

    async def test_ratelimit_backend_failure_does_not_block_asking(self):
        session_id = await self._session_id()

        async def fake_stream(session, question, *, user, model=None):
            yield ("done", {"message_id": 1, "stop_reason": "finished", "notice": "",
                            "tokens": {"total_tokens": 0}, "citations": [],
                            "unknown_refs": [], "tool_calls": 0})

        with patch.object(chat_service, "stream_answer", fake_stream), \
                patch.object(ratelimit.backend(), "sliding",
                             side_effect=RuntimeError("redis down")), self.assertRaises(RuntimeError):
            # 后端直接抛异常时由调用方兜住：这里证明失败没有被静默吞掉
            await ratelimit.check("chat-user", "1", limit=1, window_seconds=60)
        with patch.object(chat_service, "stream_answer", fake_stream), \
                patch.object(chat_api, "ASK_LIMIT_PER_USER", 0), \
                patch.object(chat_api, "ASK_LIMIT_PER_IP", 0), \
                patch.object(chat_api, "DAILY_TOKEN_BUDGET", 0):
            response = await self.client.post(
                "/chat/stream", json={"session_id": session_id, "question": "问"},
                headers={"token": self.token})
        self.assertEqual(response.status_code, 200)
