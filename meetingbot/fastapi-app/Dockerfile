FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 9090
# 不再钉 --workers 1：登录限流与问答预算的计数已搬到 Redis（common/ratelimit.py），
# 多个 worker / 多个副本共享同一份计数；未配 REDIS_URL 时会退回进程内存实现，
# 那种部署下多副本的阈值语义与单进程不同（见 DEPLOYMENT.md 的限流一节）。
# WEB_CONCURRENCY 由部署环境控制，默认 2（uvicorn 读这个变量）。
ENV WEB_CONCURRENCY=2
CMD ["sh", "-c", "python bootstrap.py && exec uvicorn main:app --host 0.0.0.0 --port 9090 --workers ${WEB_CONCURRENCY}"]
