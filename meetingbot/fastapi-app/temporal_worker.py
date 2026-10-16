"""Temporal worker 进程入口。

与 API 容器共用镜像、分开运行：API 只负责接收请求与写库，
本进程执行三类长任务的 activity（转写/纪要/自检）。

启动：python temporal_worker.py（compose 的 temporal-worker 服务）
"""

import asyncio
import logging

from services.task_queue import run_worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

if __name__ == "__main__":
    asyncio.run(run_worker())
