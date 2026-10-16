"""问答接口的传输层单测：SSE 帧、心跳、断开收尾，以及一次真实的 ASGI 往返。

这组测试不碰模型、不碰向量库，但**真的走 FastAPI 的路由与鉴权**（httpx 的 ASGI
传输直接打到路由上），因此能验证响应头、帧格式与状态码这些只在传输层成立的东西。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from api import chat as chat_api
from common.auth import create_token
from common.exception_handler import setup_exceptions
from fastapi import FastAPI
from models import User
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

        frames = chat_api._sse_frames(source, heartbeat=5)
        await frames.__anext__()
        await frames.aclose()
        self.assertTrue(source.closed, "断开时必须关闭生产端，它的收尾才有机会跑")


class _ClosableSource:
    """手写的异步迭代器：只用于观测 `aclose()` 有没有被调用。"""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
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
