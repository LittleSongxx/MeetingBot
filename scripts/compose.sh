#!/usr/bin/env sh
# 部署封装（Linux / macOS / WSL）：固定仓库根目录与 .env 位置，
# 避免在别的目录执行 docker compose 时找不到 .env 或 compose.yaml。
#
# 用法：
#   ./scripts/compose.sh up -d --build
#   ./scripts/compose.sh ps
#   ./scripts/compose.sh logs -f backend
#   ./scripts/compose.sh --profile test run --rm test
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -f "$project_dir/.env" ]; then
  echo "找不到 $project_dir/.env：请先 cp .env.example .env 并按 DEPLOYMENT.md 填写必填项。" >&2
  exit 1
fi
exec docker compose \
  --project-directory "$project_dir" \
  --env-file "$project_dir/.env" \
  -f "$project_dir/compose.yaml" \
  "$@"
