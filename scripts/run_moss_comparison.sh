#!/bin/bash
# MOSS 引擎 8 场对比（绝对路径，后台/任意 cwd 可跑）
set -a; . /home/song/code/Agent/media_agent/AIMeeting/ai_meeting/fastapi-app/.env; set +a
cd /home/song/code/Agent/media_agent/AIMeeting
PY=/home/song/code/Agent/media_agent/AIMeeting/ai_meeting/fastapi-app/.venv/bin/python
for m in "$@"; do
  echo "=== $m (moss) ==="
  PYTHONPATH=evaluation "$PY" evaluation/asr/first_test/run_first_test.py \
    --meeting "$m" --base-url http://127.0.0.1:19091 \
    --container aimeeting-local-backend-1 --tag moss --reuse-audio 2>&1 | tail -2
done
echo "ALL_MOSS_DONE"
