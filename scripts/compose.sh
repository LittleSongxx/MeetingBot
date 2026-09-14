#!/usr/bin/env sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec docker compose --project-directory "$project_dir" --env-file "$project_dir/ai_meeting/fastapi-app/.env" -f "$project_dir/compose.yaml" "$@"
