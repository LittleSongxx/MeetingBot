#!/bin/bash
# 产品启用前检查：backend 容器 → WSL host 侧车 网络与 HTTP 契约。
# 用法：bash scripts/check_sidecar_from_backend.sh [sidecar 基址，默认 http://host.docker.internal:9971]
#       bash scripts/check_sidecar_from_backend.sh <base> --post   # 另做一次 30s smoke 转写（占 GPU ~20s）
# 背景：容器内没有 curl；用 python(httpx/urllib) 检查。容器挂载了 ./evaluation → /evaluation。
#       注意 docker exec 需要 -i 才能把 heredoc 传给容器内 python。
set -uo pipefail
base="${1:-http://host.docker.internal:9971}"
post="${2:-}"
container="${BACKEND_CONTAINER:-meetingbot-local-backend-1}"

echo "=== 1) 容器存在性 ==="
docker inspect -f '{{.Name}} {{.State.Status}}' "$container" || { echo "找不到容器 $container"; exit 1; }

echo "=== 2) 容器内解析 host.docker.internal ==="
docker exec "$container" sh -lc 'getent ahostsv4 host.docker.internal || echo NO_IPV4'

echo "=== 3) 容器 → 侧车 /health ==="
docker exec -i "$container" python - "$base" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
url = base + "/health"
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read())
    print("OK", response.status, json.dumps(payload, ensure_ascii=False)[:300])
    if not payload.get("loaded"):
        print("NOTE: loaded=false（侧车未预载模型；首个 /transcribe 会先加载 ~2s）")
except Exception as error:  # noqa: BLE001
    print("FAIL", type(error).__name__, str(error)[:200])
    raise SystemExit(1)
PY
rc=$?
[ "$rc" -ne 0 ] && { echo "网络不可达：检查侧车是否绑定 0.0.0.0、端口是否一致、WSL 是否在运行"; exit "$rc"; }

if [ "$post" = "--post" ]; then
  echo "=== 4) 容器 → 侧车 真实 /transcribe（30s smoke，需 GPU）==="
  docker exec -i "$container" python - "$base" <<'PY'
import sys
import httpx

audio = "/evaluation/asr/first_test/moss_smoke.wav"
url = sys.argv[1].rstrip("/") + "/transcribe"
try:
    with open(audio, "rb") as handle:
        response = httpx.post(url, files={"file": ("moss_smoke.wav", handle, "audio/wav")},
                              data={"max_new_tokens": "2048"}, timeout=900)
    print("HTTP", response.status_code)
    payload = response.json()
    print("audio_seconds:", payload.get("audio_seconds"), "duration:", payload.get("duration"),
          "segments:", len(payload.get("segments") or []),
          "elapsed:", payload.get("elapsed_seconds"),
          "speakers:", sorted({str(s.get("speaker")) for s in (payload.get("segments") or [])}))
except Exception as error:  # noqa: BLE001
    print("FAIL", type(error).__name__, str(error)[:300])
    raise SystemExit(1)
PY
  rc=$?
  [ "$rc" -ne 0 ] && exit "$rc"
fi

echo "CHECK_SIDECAR_FROM_BACKEND_DONE"
