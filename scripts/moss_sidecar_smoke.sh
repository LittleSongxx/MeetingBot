#!/bin/bash
# MOSS 直连侧车 smoke：起侧车 → /health → 30s 样例 /transcribe → 打印响应。
# 用法：scripts/moss_sidecar_smoke.sh [端口，默认 9972]
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
# ~/.cache/huggingface 是 root 拥有（历史 sudo 运行留下），transformers 动态模块缓存
# 需要可写目录 → 用独立 HF_HOME，不碰旧缓存也不需要 sudo。
export HF_HOME="${HF_HOME:-/home/song/.cache/hf-moss}"
export HF_HUB_DISABLE_XET=1
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
port="${1:-9972}"
log=/tmp/moss_smoke_sidecar.log
wav="$repo_dir/evaluation/asr/first_test/moss_smoke.wav"
model_dir="${MOSS_MODEL:-/home/song/.cache/moss-hf/MOSS-Transcribe-Diarize}"
cd "$repo_dir"

echo "=== 启动侧车（port $port，model $model_dir，log $log）==="
MOSS_MODEL="$model_dir" PYTHONUNBUFFERED=1 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  scripts/.venv-moss/bin/python scripts/moss_asr_server.py --port "$port" --preload \
  >"$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null' EXIT

health=""
for i in $(seq 1 150); do
  health=$(curl -s -m 2 "http://127.0.0.1:$port/health" 2>/dev/null || true)
  if echo "$health" | grep -q '"loaded":true'; then
    echo "health ok: $health"
    break
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "侧车进程已退出，日志尾部：" >&2
    tail -40 "$log" >&2
    exit 1
  fi
  sleep 2
done
if ! echo "$health" | grep -q '"loaded":true'; then
  echo "侧车 300s 内未就绪，日志尾部：" >&2
  tail -40 "$log" >&2
  exit 1
fi

echo "=== smoke 转写 $wav ==="
curl -s -m 900 -X POST "http://127.0.0.1:$port/transcribe" \
  -F "file=@$wav" -F "max_new_tokens=2048" \
  -o /tmp/moss_smoke_resp.json -w "http_code=%{http_code} http_size=%{size_download} time=%{time_total}s\n"
echo "--- 响应摘要 ---"
scripts/.venv-moss/bin/python - <<'PY'
import json
try:
    payload = json.load(open("/tmp/moss_smoke_resp.json", encoding="utf-8"))
except Exception as error:  # noqa: BLE001
    print("response not JSON:", error)
    print(open("/tmp/moss_smoke_resp.json", encoding="utf-8", errors="replace").read()[:1000])
    raise SystemExit(1)
print("audio_seconds:", payload.get("audio_seconds"),
      "duration:", payload.get("duration"),
      "segments:", len(payload.get("segments") or []),
      "elapsed:", payload.get("elapsed_seconds"),
      "truncated:", payload.get("truncated"))
for seg in (payload.get("segments") or [])[:4]:
    print(" ", seg)
print("text[:200]:", (payload.get("text") or "")[:200])
print("speakers:", sorted({str(s.get("speaker")) for s in (payload.get("segments") or [])}))
PY
echo "=== 侧车日志尾部 ==="
tail -8 "$log"
echo "SMOKE_DONE"
