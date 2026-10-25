#!/bin/bash
# MOSS 侧车常驻管理（start|stop|status|log），供产品真实链路启用时使用。
# 用法：
#   bash scripts/moss_sidecar_service.sh start     # 后台常驻 + preload 模型，等 /health 就绪
#   bash scripts/moss_sidecar_service.sh status
#   bash scripts/moss_sidecar_service.sh stop
#   MOSS_PORT=9971 MOSS_PRELOAD=0 bash scripts/moss_sidecar_service.sh start   # 调试用不预载
# 说明：
# - 侧车必须绑 0.0.0.0（默认就是），产品容器经 host.docker.internal 访问；
# - 常驻进程的生命周期绑定 WSL 实例：WSL 关闭 / Windows 休眠会终止，需要重新 start；
# - 推理期间 uvicorn 事件循环被同步 generate 占用，/health 可能不响应（见报告已知限制）；
# - 日志：/tmp/moss_sidecar_resident.log；pid：/tmp/moss_sidecar_resident.pid
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export HF_HOME="${HF_HOME:-/home/song/.cache/hf-moss}"
export HF_HUB_DISABLE_XET=1
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
port="${MOSS_PORT:-9971}"
preload="${MOSS_PRELOAD:-1}"
model_dir="${MOSS_MODEL:-/home/song/.cache/moss-hf/MOSS-Transcribe-Diarize}"
pid_file=/tmp/moss_sidecar_resident.pid
log=/tmp/moss_sidecar_resident.log

running() {
  [ -f "$pid_file" ] || return 1
  local pid
  pid=$(cat "$pid_file")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

health_ok() {
  local body
  body=$(curl -s -m 2 "http://127.0.0.1:$port/health" 2>/dev/null) || return 1
  echo "$body" | grep -q '"status":"ok"' || return 1
  if [ "$preload" = "1" ]; then
    echo "$body" | grep -q '"loaded":true' || return 1
  fi
  return 0
}

case "${1:-status}" in
  start)
    if running; then
      echo "sidecar 已在运行：pid $(cat "$pid_file")，health:"
      curl -s -m 5 "http://127.0.0.1:$port/health" || echo "（无响应：可能正在推理，事件循环被占——见报告已知限制）"
      exit 0
    fi
    args=(--port "$port")
    [ "$preload" = "1" ] && args+=(--preload)
    MOSS_MODEL="$model_dir" PYTHONUNBUFFERED=1 \
      PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
      nohup scripts/.venv-moss/bin/python scripts/moss_asr_server.py "${args[@]}" \
      >>"$log" 2>&1 &
    echo $! > "$pid_file"
    echo "启动中：pid $(cat "$pid_file")，模型目录 $model_dir，preload=$preload，日志 $log"
    for _ in $(seq 1 150); do
      if health_ok; then
        echo "就绪：$(curl -s -m 3 "http://127.0.0.1:$port/health")"
        exit 0
      fi
      running || { echo "进程退出，日志尾部："; tail -30 "$log"; exit 1; }
      sleep 2
    done
    echo "300s 内未就绪，日志尾部："; tail -30 "$log"; exit 1
    ;;
  stop)
    if running; then
      pid=$(cat "$pid_file")
      kill "$pid" 2>/dev/null
      sleep 2
      echo "已停止（pid $pid）"
      rm -f "$pid_file"
    else
      echo "未在运行（或 pid 文件失效）"
      pkill -f "moss_asr_server.py --port $port" 2>/dev/null
    fi
    ;;
  status)
    if running; then
      echo "running pid $(cat "$pid_file")"
    else
      echo "not running"
    fi
    echo "--- health ---"
    curl -s -m 5 "http://127.0.0.1:$port/health" || echo "（无响应：可能正在推理，事件循环被占）"
    ;;
  log)
    tail -n "${2:-40}" "$log"
    ;;
  *)
    echo "用法：$0 {start|stop|status|log [行数]}" >&2
    exit 2
    ;;
esac
