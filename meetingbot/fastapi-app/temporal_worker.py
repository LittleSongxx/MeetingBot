"""Temporal worker 进程入口。

与 API 容器共用镜像、分开运行：API 只负责接收请求与写库，
本进程执行三类长任务的 activity（转写/纪要/自检）。

启动：python temporal_worker.py（compose 的 temporal-worker 服务）
"""

import asyncio

from common import metrics, tracing
from services.task_queue import run_worker

metrics.setup_logging()
tracing.setup_tracing()

if __name__ == "__main__":
    asyncio.run(run_worker())
