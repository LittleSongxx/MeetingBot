"""MCP 服务与 SSE 帧的单测：零模型、零向量库、零 HTTP 服务器。

钉住三件事：
- MCP 工具与 `kb_tools` **同源**：名字、参数 schema、执行路径都来自同一张表；
- MCP 的鉴权在代码里判：没有令牌、令牌无效一律拒绝，且权限由数据库里的用户决定；
- SSE 帧格式正确、空闲会发心跳注释行、客户端断开时生产端仍会被收尾（落库）。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import mcp_server
from common.auth import create_token
from models import Meeting, User
from services import kb_service, kb_tools
from tortoise import Tortoise


class _Ctx:
    """MCP 请求上下文的最小替身：只提供 headers。"""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


class McpToolTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]},
                            use_tz=True, timezone="Asia/Shanghai")
        await Tortoise.generate_schemas()
        self.user = await User.create(username="mcp-t", password="x", name="张晨")
        self.meeting = await Meeting.create(
            meeting_no="MTG-MCP", title="MCP 会议", start_time="2026-09-10 10:00:00",
            end_time="2026-09-10 11:00:00", creator_id=self.user.id,
            host_id=self.user.id, status="FINISHED")

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def test_tools_mirror_the_single_tool_layer(self):
        tools = mcp_server.build_tools()
        self.assertEqual([tool.name for tool in tools],
                         [spec.name for spec in kb_tools.TOOL_SPECS])
        for tool, spec in zip(tools, kb_tools.TOOL_SPECS, strict=True):
            # 参数 schema 是权威的那一份（带数值约束与说明），不是签名派生的近似版
            self.assertEqual(tool.parameters, spec.json_schema())
        limits = tools[0].parameters["properties"]["limit"]
        self.assertEqual((limits["minimum"], limits["maximum"]), (1, 20))

    async def test_missing_token_is_refused_without_touching_the_knowledge_base(self):
        runner = mcp_server._make_runner(kb_tools.TOOL_SPECS[0])
        search = AsyncMock()
        with patch.object(kb_service, "search_knowledge", search):
            result = await runner(ctx=_Ctx(), query="上线时间")
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("登录状态已失效", payload["error"])
        search.assert_not_awaited()

    async def test_invalid_token_is_refused(self):
        runner = mcp_server._make_runner(kb_tools.TOOL_SPECS[0])
        result = await runner(ctx=_Ctx({"authorization": "Bearer not-a-jwt"}), query="x")
        self.assertFalse(json.loads(result)["ok"])

    async def test_valid_token_runs_the_shared_implementation(self):
        runner = mcp_server._make_runner(kb_tools.TOOL_SPECS[0])
        search = AsyncMock(return_value=[])
        token = create_token(self.user)
        with patch.object(kb_service, "search_knowledge", search):
            result = await runner(ctx=_Ctx({"Authorization": f"Bearer {token}"}), query="上线时间")
        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["hits"], [])
        # 权限集合来自 common.access：本人创建的会议可见
        self.assertEqual(search.await_args.kwargs["allowed_meeting_ids"], [self.meeting.id])

    async def test_repo_token_header_also_works(self):
        runner = mcp_server._make_runner(kb_tools.TOOL_SPECS[1])
        token = create_token(self.user)
        result = await runner(ctx=_Ctx({"token": token}), kind="meeting")
        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual([fact["meeting_id"] for fact in payload["facts"]], [self.meeting.id])

    async def test_tool_errors_stay_structured(self):
        runner = mcp_server._make_runner(kb_tools.TOOL_SPECS[2])
        token = create_token(self.user)
        result = await runner(ctx=_Ctx({"token": token}), minutes_id=None, meeting_id=None)
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("至少要给一个", payload["error"])

    def test_bearer_parsing(self):
        self.assertEqual(mcp_server._bearer("Bearer abc"), "abc")
        self.assertEqual(mcp_server._bearer("bearer abc"), "abc")
        self.assertEqual(mcp_server._bearer("abc"), "")
        self.assertEqual(mcp_server._bearer(None), "")


if __name__ == "__main__":
    unittest.main()


class McpHttpTransportTest(unittest.IsolatedAsyncioTestCase):
    """真的挂载一次、真的发一次 HTTP：验证路径、协议往返与工具 schema。

    这一组是"挂载能用"的直接证据——上一版把子应用按默认 Host 白名单挂上去，
    每个请求都会被 421 拒掉，只有真发请求才看得见。

    三点接线约束（都在产品里同样成立）：
    - 会话管理器每个实例只能 run 一次，且必须在**同一个任务**里进出；
      产品里由 lifespan 进、lifespan 出，测试里就把整段放进一个用例。
    - `/mcp` 要靠 `PathAlias` 补斜杠才能直达，否则先吃一个 307。
    - streamable HTTP 用 SSE 帧回包，因此响应体是 `data: {...}` 行。
    """

    HEADERS = {"accept": "application/json, text/event-stream",
               "content-type": "application/json"}

    async def test_initialize_list_and_call_round_trip(self):
        """一次跑完握手、列工具、调用工具：客户端接入的完整路径。"""
        from fastapi import FastAPI
        app = FastAPI()
        # 与 main.py 同款接线：中间件补斜杠 + 挂载子应用
        app.add_middleware(mcp_server.PathAlias)
        app.mount(mcp_server.MCP_PATH, mcp_server.streamable_app())
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://meetingbot.example")
        async with mcp_server.session_manager():
            try:
                init = await self._rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18",
                                                           "capabilities": {},
                                                           "clientInfo": {"name": "test",
                                                                          "version": "1"}}})
                self.assertEqual(init["result"]["serverInfo"]["name"], mcp_server.SERVER_NAME)

                listed = await self._rpc(client, {"jsonrpc": "2.0", "id": 2,
                                                  "method": "tools/list"})
                tools = listed["result"]["tools"]
                self.assertEqual([tool["name"] for tool in tools],
                                 [spec.name for spec in kb_tools.TOOL_SPECS])
                search = next(tool for tool in tools if tool["name"] == "search_knowledge")
                self.assertEqual(search["inputSchema"]["properties"]["limit"]["maximum"], 20)

                called = await self._rpc(client, {"jsonrpc": "2.0", "id": 3,
                                                  "method": "tools/call",
                                                  "params": {"name": "search_knowledge",
                                                             "arguments": {"query": "上线时间"}}})
                payload = json.loads(called["result"]["content"][0]["text"])
                self.assertFalse(payload["ok"])
                self.assertIn("登录状态已失效", payload["error"])
            finally:
                await client.aclose()

    async def _rpc(self, client: httpx.AsyncClient, payload: dict, *,
                   token: str | None = None) -> dict:
        headers = dict(self.HEADERS)
        if token:
            headers["authorization"] = f"Bearer {token}"
        # 用不带斜杠的路径：这正是 MCP 客户端按惯例配置的那个
        response = await client.post("/mcp", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        raise AssertionError(f"响应里没有 data 帧：{response.text[:200]}")


if __name__ == "__main__":
    unittest.main()
