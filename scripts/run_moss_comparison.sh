#!/bin/bash
# MOSS 引擎 8 场对比（基于脚本位置定位仓库根，后台/任意 cwd 可跑）
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
set -a; . "$repo_dir/meetingbot/fastapi-app/.env"; set +a
cd "$repo_dir"
PY="$repo_dir/meetingbot/fastapi-app/.venv/bin/python"
for m in "$@"; do
  echo "=== $m (moss) ==="
  PYTHONPATH=evaluation "$PY" evaluation/asr/first_test/run_first_test.py \
    --meeting "$m" --base-url http://127.0.0.1:19091 \
    --container meetingbot-local-backend-1 --tag moss --reuse-audio 2>&1 | tail -2
done
echo "ALL_MOSS_DONE"
