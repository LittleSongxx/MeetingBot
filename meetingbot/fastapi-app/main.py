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
    from models import AiModelConfig
    from services.agent_service import recover_agent_runs
    from services.kb_service import recover_kb_index_tasks
    from services.minutes_service import recover_minutes_tasks
    from services.transcription_service import recover_transcription_tasks
    for config in await AiModelConfig.filter(model_type__in=("MINUTES", "AGENT", "SPEAKER"), enabled=True):
        strategy = providers.structured_output_strategy(config.base_url)
        if strategy != providers.JSON_SCHEMA:
            raise RuntimeError(
                f"模型配置 {config.name}（{config.base_url}）不支持 strict json_schema，"
                "主力模型请使用支持约束解码的供应商（当前策略 "
                f"{strategy}，仅靠 re-ask 兜底）")

    # 兜底扫尾：重新入队"建了行但 workflow 未启动"的任务（写行与 start_workflow
    # 之间的窄窗口）。正常崩溃续跑由 Temporal 原生完成，不经过这里。
    await recover_transcription_tasks()
    await recover_minutes_tasks()
    await recover_agent_runs()
    await recover_kb_index_tasks()
    # MCP 的会话管理器要由**父应用**的 lifespan 进入：子应用的 lifespan 在挂载后
    # 不会被执行，少了这一步第一次调用就会报"session manager 没启动"。
    async with mcp_server.session_manager():
        # yield 之后服务才开始接收请求，本项目在关闭阶段没有要做的事，所以 yield 后面没有代码
        yield


# 把生命周期钩子交给 FastAPI，应用实例一创建就带上启动时的恢复动作
app = FastAPI(lifespan=lifespan)

# 跨域配置 CORS
# CORS 来源由 CORS_ALLOW_ORIGINS 控制（逗号分隔）；缺省 "*" 仅为保持本地开发行为，
# 对外部署应在 .env 收紧为实际前端来源（同源反代部署甚至可以留空）。
_cors = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors.split(",") if o.strip()] or ["*"],
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
