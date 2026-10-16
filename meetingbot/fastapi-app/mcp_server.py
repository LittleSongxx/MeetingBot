"""MCP 服务：把知识库工具以 Model Context Protocol 暴露给外部客户端。

## 一个工具层、两种传输

MCP 的工具**不是**第二份实现：每个工具都从 `kb_tools.TOOL_SPECS` 派生，
执行走同一个 `kb_tools.execute_tool`，权限判在 `common.access`（同一个 Actor）。
这里只做三件传输侧的事：把 HTTP 请求头里的 JWT 换成 `Actor`、把结果序列化成
text content、把异常包成可读错误。

## 为什么挂进现有 FastAPI 而不是独立进程

- 中间件（CORS、限流、追踪）随父应用自动继承，独立进程要再配一遍；
- 鉴权复用同一个 JWT 校验函数（含密码指纹 `pv`），不存在"MCP 侧忘了校验 pv"
  这种只在一条路径上出现的漏洞；
- 部署形态不变（还是那六个容器），少一个进程就少一处要监控的东西。

代价是 MCP 的会话状态与 Web 进程同生命周期——因此用 `stateless_http=True`：
每个请求自成一次会话，不留服务端状态，多副本部署也不粘会话。

## 鉴权：在代码里判，不靠提示词

MCP 客户端在请求头里带 JWT（`Authorization: Bearer <token>`，或沿用本产品的
`token` 头）。**请求头是客户端可控输入**，所以它只被当作凭据交给 `user_from_token`
验签，验完再按数据库里的用户判权限——与 HTTP 接口是同一条路径。
把"你是管理员"这种话写在工具描述里是无效防御：模型可以不理它。

头名按小写归一后再取：HTTP 头名大小写不敏感，而取头这一步拿到的是映射，
不同传输给的映射大小写形态并不一致（实测 MCP 侧给的是原始大小写）。
"""

from __future__ import annotations

import inspect
import logging
import os
from typing import Annotated, Any, Awaitable, Callable

from common.auth import user_from_token
from common.exception_handler import CustomException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from services import kb_tools
from starlette.applications import Starlette

logger = logging.getLogger("meetingbot.mcp")

SERVER_NAME = "meetingbot-kb"
MCP_PATH = "/mcp"

INSTRUCTIONS = (
    "会议知识库问答工具。可以检索会议转写与已确认纪要、查询结构化事实"
    "（决策/待办/风险/会议清单）、取某场会的完整纪要、按时间窗取转写原文。"
    "所有工具都会按调用者的会议可见性过滤，因此不会返回无权访问的会议内容。"
)

_server: MCPServer | None = None
_app: Starlette | None = None


def build_tools() -> list[Tool]:
    """从工具表派生 MCP 工具。参数 schema 用的是**同一份** pydantic 模型。"""
    return [_make_tool(spec) for spec in kb_tools.TOOL_SPECS]


def _make_tool(spec: kb_tools.ToolSpec) -> Tool:
    run = _make_runner(spec)
    tool = Tool.from_function(run, name=spec.name, description=spec.description)
    # 参数 schema 换成 kb_tools 那份**权威**的：MCP 从函数签名派生出来的这份
    # 少了 Field 上的数值约束（实测 ge/le 不进 schema）。函数签名负责"参数叫什么、
    # 怎么校验"，schema 负责"给客户端看什么"——两者都源自同一批 Field 定义。
    tool.parameters = spec.json_schema()
    return tool


def _make_runner(spec: kb_tools.ToolSpec) -> Callable[..., Awaitable[str]]:
    async def run(*, ctx: Context, **kwargs: Any) -> str:
        try:
            actor = await _actor_from(ctx)
        except CustomException as error:
            # 鉴权失败对客户端是"这次调用不成立"，不是服务端故障
            return kb_tools.tool_result_text({"ok": False, "error": error.message})
        result = await kb_tools.execute_tool(actor, spec.name, kwargs)
        return kb_tools.tool_result_text(result)

    # 把参数名、类型与默认值写成真实签名：MCP 靠它做参数校验与 schema 派生，
    # 而它们逐字来自 spec.arguments 的字段定义（不在这里重抄一遍）
    run.__signature__ = _signature_for(spec.arguments)
    run.__name__ = spec.name
    return run


def _signature_for(model: type) -> inspect.Signature:
    parameters: list[inspect.Parameter] = [inspect.Parameter(
        "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
    for name, field in model.model_fields.items():
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY,
            annotation=_annotated(field),
            default=inspect.Parameter.empty if field.is_required() else field.default,
        ))
    return inspect.Signature(parameters)


def _annotated(field: Any) -> Any:
    """把 pydantic 字段的说明与约束搬进类型注解（MCP 只会看注解）。"""
    metadata = []
    if field.description:
        metadata.append(Field(description=field.description))
    for name in ("ge", "le", "min_length", "max_length", "pattern"):
        value = getattr(field, name, None)
        if value is not None:
            metadata.append(Field(**{name: value}))
    if not metadata:
        return field.annotation
    return Annotated.__class_getitem__((field.annotation, *metadata))


async def _actor_from(ctx: Context) -> kb_tools.Actor:
    return await kb_tools.Actor.resolve(await _user_from_context(ctx))


async def _user_from_context(ctx: Context):
    headers = {str(key).lower(): value for key, value in (ctx.headers or {}).items()}
    token = _bearer(headers.get("authorization")) or str(headers.get("token") or "").strip()
    return await user_from_token(token or None)


def _bearer(value: str | None) -> str:
    if not value:
        return ""
    prefix, _, rest = str(value).partition(" ")
    return rest.strip() if prefix.lower() == "bearer" else ""


def server() -> MCPServer:
    """本进程唯一的 MCP 服务实例（工具表在进程内共享）。"""
    global _server
    if _server is None:
        _server = MCPServer(
            name=SERVER_NAME, instructions=INSTRUCTIONS, tools=build_tools(),
        )
    return _server


def streamable_app() -> Starlette:
    """挂到 `/mcp` 的 ASGI 子应用（进程内单例）。

    路径写成 `/` 是因为挂载点已经带了 `/mcp`（Starlette 挂载会把前缀剥掉）；
    默认值 `/mcp` 会让真实路径变成 `/mcp/mcp`。

    `stateless_http=True`：每个请求自成一次会话，服务端不留会话状态——
    多副本部署不需要粘会话，进程重启也不会丢掉"客户端以为还在"的会话。

    **做成单例有实际原因**：每次调用都会新建一个会话管理器，而 lifespan 钩子里
    取的 `session_manager()` 永远是最新的那一个。多调一次就会让先返回的那个
    子应用挂在一个再也不会被启动的会话管理器上，第一次调用才报错。
    """
    global _app
    if _app is None:
        _app = server().streamable_http_app(
            streamable_http_path="/", stateless_http=True,
            transport_security=_transport_security(),
        )
    return _app


def _transport_security() -> TransportSecuritySettings:
    """Host 头白名单（DNS rebinding 防护）。

    **必须显式传**：库只在 host 参数是回环地址时才自动开防护，而一旦开，
    允许的 Host 只有 `127.0.0.1:*` / `localhost:*` / `[::1]:*`。本项目是容器部署、
    经反向代理访问，真实的 Host 是域名——用默认值会让**每一个** MCP 请求
    都被 421 拒掉（实测确认过）。

    因此白名单由 `MCP_ALLOWED_HOSTS`（逗号分隔）提供：设了就开防护并只放行这些
    Host；没设就关掉。关掉是可接受的默认，因为工具**每一个**都要 JWT 鉴权才可能
    取到数据（见模块文档的鉴权一节），DNS rebinding 在这里能造成的最坏结果是
    "攻击者的页面替用户发了一个他本来就能发的请求"。
    """
    hosts = [item.strip() for item in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if item.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def session_manager():
    """MCP 会话管理器的生命周期上下文。

    **必须**由父应用的 lifespan 进入：子应用的 lifespan 在挂载后不会被执行，
    少了这一步，第一次调用就会报"session manager 没启动"。
    这也意味着 `streamable_app()` 必须先被调用过（会话管理器由它创建）。
    """
    return server().session_manager.run()


class PathAlias:
    """把恰好等于 `/mcp` 的请求路径改写成 `/mcp/`（纯 ASGI 中间件）。

    为什么需要：Starlette 的 `Mount` 用的正则匹配是 `挂载点 + "/{path:path}"`，
    也就是说 `/mcp` 本身不被匹配，父路由会先回一个 307 到 `/mcp/`。而 MCP 客户端
    按惯例配置的就是 `https://host/mcp`——多一跳重定向意味着"客户端跟不跟随"
    成了部署能不能用的隐藏前提。这里在进入路由之前补一个斜杠，两种写法都直达。

    写成纯 ASGI 而不是 `BaseHTTPMiddleware`：后者会把响应包进额外的任务与内存流，
    本项目那条逐 token 的 SSE 经不起这种折腾。
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == MCP_PATH:
            scope = {**scope, "path": MCP_PATH + "/", "raw_path": (MCP_PATH + "/").encode()}
        await self.app(scope, receive, send)
