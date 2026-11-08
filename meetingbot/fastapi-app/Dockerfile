# 后端镜像：Python 3.12 + FFmpeg（音视频切片）+ 中文字体（Word/PDF 导出）
#
# 两个阶段：
#   backend —— 生产/本地运行阶段，compose 的 backend 服务用它；
#   test    —— backend + requirements-dev.txt，compose 的 test 服务
#              （profiles: ["test"]）用它跑单测，CI 里等价命令见 .github/workflows/ci.yml。
FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ffmpeg：转写前把录音/视频按 CHUNK_SECONDS 切片；
# fonts-noto-cjk：PDF 导出探测 /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 依赖层单独 COPY：只改源码时不会触发重新 pip install。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 单进程是硬约束：转写/纪要/自检三类耗时任务都跑在本进程内
# （asyncio.create_task + 内存字典去重 + 启动时 recover_*），
# 多 worker 或多副本会把同一个任务领取多次，因此固定 --workers 1。
# 数据库表结构与种子数据由 db 镜像负责初始化，应用启动只负责提供接口。
EXPOSE 9090
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9090", "--workers", "1"]

# ---------------- 测试阶段：backend + 开发依赖 ----------------
FROM backend AS test

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# 单测使用 sqlite 内存库与 mock 模型，不需要 MySQL、FFmpeg 外网或模型密钥。
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]
