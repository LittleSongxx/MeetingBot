from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn
from starlette.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise

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

    # 依次恢复三类异步任务，全部执行完才放行 yield，也就是服务对外提供接口之前就已经恢复完毕
    await recover_transcription_tasks()
    await recover_minutes_tasks()
    await recover_agent_runs()
    # yield 之后服务才开始接收请求，本项目在关闭阶段没有要做的事，所以 yield 后面没有代码
    yield


# 把生命周期钩子交给 FastAPI，应用实例一创建就带上启动时的恢复动作
app = FastAPI(lifespan=lifespan)

# 跨域配置 CORS
app.add_middleware(
    CORSMiddleware,
    # 允许所有源
    allow_origins=["*"],
    # 允许所有 HTTP 方法
    allow_methods=["*"],
    # 允许所有请求头
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


if __name__ == "__main__":
    uvicorn.run("main:app", host="localhost", reload=True, port=9090)
