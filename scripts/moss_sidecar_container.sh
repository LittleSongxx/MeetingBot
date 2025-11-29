#!/bin/bash
# 本机（Docker Desktop + WSL2）用已装好的 venv 直接跑 MOSS 侧车容器，避开镜像构建下载。
# 可移植部署请用 compose profile：./scripts/compose.sh --profile moss up -d asr-sidecar
#
# 前提：WSL 的 scripts/.venv-moss 已装好依赖（含 imageio-ffmpeg fallback），
#       权重在 C:/moss-model（或改 MODEL_HOST）。
set -eu
REPO=/home/song/code/Agent/media_agent/AIMeeting
MODEL_HOST=${MOSS_MODEL_HOST_DIR:-C:/moss-model}
NAME=${MOSS_SIDECAR_NAME:-moss-asr-local}
PORT=${MOSS_SIDECAR_PORT:-9971}
NETWORK=${MOSS_SIDECAR_NETWORK:-meetingbot-local_default}
UV_WIN='\\wsl.localhost\Ubuntu-22.04\home\song\.local\share\uv\python'
SCRIPTS_WIN='\\wsl.localhost\Ubuntu-22.04\home\song\code\Agent\media_agent\AIMeeting\scripts'

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "找不到 Docker 网络 $NETWORK；先 ./scripts/compose.sh up -d" >&2
  exit 1
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --gpus all \
  --network "$NETWORK" --network-alias asr-sidecar \
  -e MOSS_MODEL=/models -e MOSS_DEVICE=cuda:0 -e MOSS_DTYPE=bf16 -e TZ=Asia/Shanghai \
  -v "$MODEL_HOST:/models:ro" \
  -v "$UV_WIN:/home/song/.local/share/uv/python:ro" \
  -v "$SCRIPTS_WIN:$REPO/scripts:ro" \
  nvidia/cuda:12.3.1-base-ubuntu22.04 \
  "$REPO/scripts/.venv-moss/bin/python" "$REPO/scripts/moss_asr_server.py" --port "$PORT" --preload
echo "started $NAME; 等 health:"
for i in $(seq 1 40); do
  if docker exec -i meetingbot-local-backend-1 python -c \
      "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://asr-sidecar:9971/health',timeout=3) else 1)" >/dev/null 2>&1; then
    echo "healthy after $((i*2))s"; break
  fi
  sleep 2
done
docker exec -i meetingbot-local-backend-1 python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://asr-sidecar:9971/health", timeout=5).read().decode()[:200])
PY
