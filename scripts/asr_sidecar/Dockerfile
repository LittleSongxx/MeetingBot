FROM nvidia/cuda:12.3.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MOSS_MODEL=/models \
    MOSS_DEVICE=cuda:0

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-moss.txt /app/requirements-moss.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        -r /app/requirements-moss.txt

COPY moss_asr_server.py /app/moss_asr_server.py

EXPOSE 9971
CMD ["python3", "moss_asr_server.py", "--port", "9971", "--preload"]
