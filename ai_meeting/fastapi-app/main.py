from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
import uvicorn
from starlette.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise
from tortoise import connections

from api import api_router
from common.exception_handler import setup_exceptions

from common.result import Result
from settings import TORTOISE_ORM


# 应用生命周期钩子，yield 之前的代码在服务启动时执行一次，yield 之后的代码在服务关闭时执行
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 三个恢复函数分别属于转写、纪要、纪要自检三个服务，在钩子函数内部导入
    from services.agent_service import recover_agent_runs
    from services.minutes_service import recover_minutes_tasks
    from services.transcription_service import recover_transcription_tasks

    # 结构化输出能力断言：主 LLM 配置必须走 strict json_schema（约束解码）。
    # 只支持 json_object 的供应商（如 DeepSeek）靠 invoke_json 的 re-ask 兜底，
    # 但主力路径应为约束解码——启动即校验，配置漂移当场暴露而不是运行期偶发。
    from common import providers
    from models import AiModelConfig
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
    # yield 之后服务才开始接收请求，本项目在关闭阶段没有要做的事，所以 yield 后面没有代码
    yield


# 把生命周期钩子交给 FastAPI，应用实例一创建就带上启动时的恢复动作
app = FastAPI(lifespan=lifespan)

# 跨域配置 CORS
# CORS 来源由 CORS_ALLOW_ORIGINS 控制（逗号分隔）；缺省 "*" 仅为保持本地开发行为，
# 对外部署应在 .env 收紧为实际前端来源（同源反代部署甚至可以留空）。
import os as _os
_cors = _os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors.split(",") if o.strip()] or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 配置路由
app.include_router(api_router)
# 注册orm
register_tortoise(app, config=TORTOISE_ORM, add_exception_handlers=True)
# 注册异常处理器
setup_exceptions(app)


@app.get("/")
async def root():
    return Result.success()


@app.get("/health")
async def health():
    try:
        await connections.get("default").execute_query("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="localhost", reload=True, port=9090)
