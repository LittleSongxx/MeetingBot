#!/usr/bin/env bash
set -euo pipefail
cd /home/song/code/Agent/media_agent/AIMeeting/evaluation
for r in run1 run2 run3; do
  echo "===== $r ====="
  .venv-ragas/bin/python -u judges/ragas_official.py \
    --run-label "$r" \
    --out "results/ragas_official_${r}.json" \
    --authorized-llm-calls 1200 \
    --concurrency 20 \
    --batch-size 10
done
