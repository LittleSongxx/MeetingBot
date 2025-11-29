#!/bin/bash
# MOSS 直连侧车 8 场 cpCER 复测驱动（每场重启侧车，避免长音频显存碎片/状态残留）。
# 用法：scripts/run_moss_direct_8.sh [meeting ...]   # 缺省跑全部 8 场
#       scripts/run_moss_direct_8.sh --aggregate-only
# 产物：evaluation/asr/first_test/moss_direct/（raw / task / cpcer / single / report）
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export HF_HOME="${HF_HOME:-/home/song/.cache/hf-moss}"
export HF_HUB_DISABLE_XET=1
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
port="${MOSS_PORT:-9971}"
out_dir="$repo_dir/evaluation/asr/first_test/moss_direct"
log_dir=/tmp/moss_direct_logs
mkdir -p "$out_dir" "$log_dir"
tag="${MOSS_TAG:-moss_direct}"
model_dir="${MOSS_MODEL:-/home/song/.cache/moss-hf/MOSS-Transcribe-Diarize}"
py_eval="$repo_dir/evaluation/.venv/bin/python"
all_meetings=(R8001_M8004 R8003_M8001 R8007_M8010 R8007_M8011 R8008_M8013 R8009_M8018 R8009_M8019 R8009_M8020)

if [ "${1:-}" = "--aggregate-only" ]; then
  "$py_eval" scripts/moss_direct_cpcor.py --aggregate-only --out-dir "$out_dir" --tag "$tag"
  exit $?
fi
meetings=("$@")
if [ ${#meetings[@]} -eq 0 ]; then
  meetings=("${all_meetings[@]}")
fi

# 预处理：8 通道 → 单声道 16k（幂等）
scripts/prepare_moss_audio.sh >/dev/null

for meeting in "${meetings[@]}"; do
  log="$log_dir/sidecar_$meeting.log"
  echo "=== $meeting：启动侧车（log $log）==="
  MOSS_MODEL="$model_dir" PYTHONUNBUFFERED=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    scripts/.venv-moss/bin/python scripts/moss_asr_server.py --port "$port" --preload \
    >"$log" 2>&1 &
  pid=$!
  loaded=0
  health=""
  for i in $(seq 1 150); do
    health=$(curl -s -m 2 "http://127.0.0.1:$port/health" 2>/dev/null || true)
    if echo "$health" | grep -q '"loaded":true'; then loaded=1; break; fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 2
  done
  if [ "$loaded" != 1 ]; then
    echo "!! $meeting 侧车未就绪，日志尾部："
    tail -30 "$log"
    echo "{\"meeting\": \"$meeting\", \"status\": \"SIDECAR_FAILED\"}" \
      > "$out_dir/single_${meeting}.${tag}.json"
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    continue
  fi
  gpu_before=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)
  started=$(date +%s)
  "$py_eval" scripts/moss_direct_cpcor.py --meeting "$meeting" \
    --sidecar-url "http://127.0.0.1:$port" --out-dir "$out_dir" --tag "$tag"
  rc=$?
  elapsed=$(( $(date +%s) - started ))
  gpu_after=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)
  echo "=== $meeting：rc=$rc，用时 ${elapsed}s，GPU ${gpu_before} → ${gpu_after} ==="
  tail -3 "$log" | sed 's/^/  [sidecar] /'
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  sleep 3
done

echo "=== 汇总 8 场 ==="
"$py_eval" scripts/moss_direct_cpcor.py --aggregate-only --out-dir "$out_dir" --tag "$tag"
echo "RUN_MOSS_DIRECT_DONE"
