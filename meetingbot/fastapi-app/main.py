import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from tortoise import connections
from tortoise.contrib.fastapi import register_tortoise

from api import api_router
from common.exception_handler import setup_exceptions
from common.logging_conf import setup_logging
from common.result import Result
from settings import CORS_ALLOW_ORIGINS, TORTOISE_ORM

# 日志在导入期装好：容器启动后的头几行就必须是结构化日志，
# 否则排查启动问题时正好缺最需要的那一段
setup_logging()
logger = logging.getLogger(__name__)


# 应用生命周期钩子，yield 之前的代码在服务启动时执行一次，yield 之后的代码在服务关闭时执行
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 三个恢复函数分别属于转写、纪要、纪要自检三个服务，在钩子函数内部导入
    from services.agent_service import recover_agent_runs
    from services.minutes_service import recover_minutes_tasks
    from services.transcription_service import recover_transcription_tasks

    # 依次恢复三类异步任务，全部执行完才放行 yield，也就是服务对外提供接口之前就已经恢复完毕
    transcription_count = await recover_transcription_tasks()
    minutes_count = await recover_minutes_tasks()
    agent_count = await recover_agent_runs()
    logger.info(
        "异步任务恢复完成",
        extra={"transcription": transcription_count, "minutes": minutes_count, "agent": agent_count},
    )
    yield
    logger.info("服务已关闭")


# 把生命周期钩子交给 FastAPI，应用实例一创建就带上启动时的恢复动作
app = FastAPI(lifespan=lifespan)

# 跨域配置：同源部署时 CORS_ALLOW_ORIGINS 为空，不安装中间件；
# 本地前端 dev server 跨域访问时由 .env 指定白名单，不再无脑放开 *
if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
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
    """探活接口：数据库可用才算健康。容器 healthcheck 依赖这个 200/503。"""
    try:
        await connections.get("default").execute_query("SELECT 1")
    except Exception:
        logger.exception("健康检查失败：数据库不可用")
        return JSONResponse(status_code=503, content={"status": "unhealthy", "db": "down"})
    return JSONResponse(status_code=200, content={"status": "ok", "db": "up"})


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", reload=True, port=9090)
