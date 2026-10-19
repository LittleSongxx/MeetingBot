import os
from contextlib import asynccontextmanager

import mcp_server
import uvicorn
from api import api_router
from common import metrics, tracing
from common.exception_handler import setup_exceptions
from common.result import Result
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from settings import TORTOISE_ORM
from starlette.middleware.cors import CORSMiddleware
from tortoise import connections
from tortoise.contrib.fastapi import register_tortoise

# 日志与追踪在**导入期**装好：容器启动后头几行日志就必须是结构化的，
# 否则排查启动问题时正好缺最需要的那一段
metrics.setup_logging()
tracing.setup_tracing()


# 应用生命周期钩子，yield 之前的代码在服务启动时执行一次，yield 之后的代码在服务关闭时执行
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 四个恢复函数分别属于转写、纪要、纪要自检、知识库四个服务，在钩子函数内部导入
    # 结构化输出能力断言：主 LLM 配置必须走 strict json_schema（约束解码）。
    # 只支持 json_object 的供应商（如 DeepSeek）靠 invoke_json 的 re-ask 兜底，
    # 但主力路径应为约束解码——启动即校验，配置漂移当场暴露而不是运行期偶发。
    from common import providers
    from models import AiModelConfig, ModelRoute
    from services import chat_checkpoint, chat_events, kb_outbox, pg_kb
    from services.agent_service import recover_agent_runs
    from services.kb_service import recover_kb_index_tasks
    from services.minutes_service import recover_minutes_tasks
    from services.model_config import STRUCTURED_ROUTE_KEYS
    from services.transcription_service import recover_transcription_tasks
    for config in await AiModelConfig.filter(model_type__in=("MINUTES", "AGENT", "SPEAKER"), enabled=True):
        strategy = providers.structured_output_strategy(config.base_url)
        if strategy != providers.JSON_SCHEMA:
            raise RuntimeError(
                f"模型配置 {config.name}（{config.base_url}）不支持 strict json_schema，"
                "主力模型请使用支持约束解码的供应商（当前策略 "
                f"{strategy}，仅靠 re-ask 兜底）")

    # 路由目标也要过同一道结构化输出能力校验：把 CHEAP 模型路由到纪要/自检/
    # 规划/摘要上时，若它不支持 strict json_schema，宁可启动失败也不要在运行期
    # 变成"模型不听话"的假象。CHAT 走工具调用，不在结构化校验范围内。
    for route in await ModelRoute.filter(enabled=True):
        if route.route_key not in STRUCTURED_ROUTE_KEYS:
            continue
        routed = await AiModelConfig.filter(id=route.model_config_id, enabled=True).first()
        if routed is None:
            raise RuntimeError(f"模型路由 {route.route_key} 的目标配置不存在或未启用")
        strategy = providers.structured_output_strategy(routed.base_url)
        if strategy != providers.JSON_SCHEMA:
            raise RuntimeError(
                f"模型路由 {route.route_key} 的目标 {routed.name} 不支持 strict json_schema")

    # PG 同时承载检索索引与 checkpoint：不可用时检索关闭、checkpoint 不启用，
    # 但不阻断服务启动（无检索依据的问答由 no_retrieval 闸标注）。
    await pg_kb.init()
    await chat_checkpoint.init()
    # 兜底扫尾：重新入队"建了行但 workflow 未启动"的任务（写行与 start_workflow
    # 之间的窄窗口）。正常崩溃续跑由 Temporal 原生完成，不经过这里。
    await recover_transcription_tasks()
    await recover_minutes_tasks()
    await recover_agent_runs()
    await recover_kb_index_tasks()
    await kb_outbox.reconcile_all()
    # MCP 的会话管理器要由**父应用**的 lifespan 进入：子应用的 lifespan 在挂载后
    # 不会被执行，少了这一步第一次调用就会报"session manager 没启动"。
    async with mcp_server.session_manager():
        try:
            yield
        finally:
            await chat_events.close()
            await chat_checkpoint.close()
            await pg_kb.close()


# 把生命周期钩子交给 FastAPI，应用实例一创建就带上启动时的恢复动作
app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """给每个 HTTP 请求一个应用级 trace id，并在响应头回传。

    OTel 未启用时它仍然有效：模型/工具调用把同一个 id 落 ai_call_log，
    排查一次请求时不必依赖外部 trace 后端。
    """
    trace_id = (request.headers.get("X-Trace-Id") or "").strip() or tracing.new_trace_id()
    token = tracing.set_trace_id(trace_id)
    try:
        response = await call_next(request)
    finally:
        tracing.reset_trace_id(token)
    response.headers["X-Trace-Id"] = trace_id
    return response

# 跨域配置 CORS
# 未设置 CORS_ALLOW_ORIGINS 时保持本地开发的 "*"；显式设为空串表示
# “同源部署、不允许跨域”，不能再被 `or ["*"]` 静默放大成通配符。
_cors_env = os.getenv("CORS_ALLOW_ORIGINS")
_cors = ["*"] if _cors_env is None else [
    item.strip() for item in _cors_env.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 配置路由
app.include_router(api_router)
# MCP 子应用挂到 /mcp：同一组知识库工具的第二种传输（工具实现在 services/kb_tools，
# 这里只挂载）。挂载点写在路由注册之后——它是一个前缀路由，越靠后越不容易挡住
# 其他路径；鉴权不在这一层做，而在每个工具内部按 JWT 判权。
# 上面那层 PathAlias 让 /mcp 与 /mcp/ 都能直达（否则默认会先回一个 307）。
app.add_middleware(mcp_server.PathAlias)
app.mount(mcp_server.MCP_PATH, mcp_server.streamable_app())
# 注册orm
register_tortoise(app, config=TORTOISE_ORM, add_exception_handlers=True)
# 注册异常处理器
setup_exceptions(app)


@app.get("/")
async def root():
    return Result.success()


@app.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus 文本格式（指标名与 OTel 语义约定的对应见 common/metrics.py）。

    默认没有额外鉴权：指标里没有会议内容，且项目默认把后端端口绑在本机回环上，
    由部署方的网络策略保护。要把采集器放到别的机器上时设 `METRICS_TOKEN`，
    设了之后没有匹配的请求头一律 403——不这么做就等于把内部用量暴露在网络上。
    """
    expected = os.getenv("METRICS_TOKEN", "").strip()
    if expected and request.headers.get("x-metrics-token") != expected:
        raise HTTPException(status_code=403, detail="metrics token required")
    return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
async def health():
    try:
        await connections.get("default").execute_query("SELECT 1")
    except Exception as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="localhost", reload=True, port=9090)
