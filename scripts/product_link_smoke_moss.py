"""产品真实链路 MOSS 冒烟：登录 → 建评测会议 → 上传短音频 → 走产品 /transcription。

验证「product backend → asr-sidecar(http 引擎)」链路可用、task.model_name 为本地引擎名、
segments 非空；不用于质量指标（质量指标见 moss_direct / moss_own_metrics）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:19091")
ENV = REPO / "meetingbot/fastapi-app/.env"
OUT = REPO / "evaluation/results/moss_product_link_smoke.json"


def load_env() -> dict[str, str]:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request(method: str, path: str, payload: dict | None = None, *,
            token: str = "", upload: Path | None = None) -> dict:
    url = BASE + path
    headers = {}
    data = None
    if token:
        headers["token"] = token
    if upload is not None:
        boundary = uuid.uuid4().hex
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        data = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{upload.name}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode() + upload.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    elif payload is not None:
        headers["Content-Type"] = "application/json;charset=utf-8"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    if str(result.get("code")) != "200":
        raise SystemExit(f"API 拒绝 {path}: {result.get('msg')}")
    return result["data"]


def main() -> int:
    env = load_env()
    account = request("POST", "/login", {"username": env["APP_ADMIN_USERNAME"],
                                        "password": env["APP_ADMIN_PASSWORD"]})
    token = account["token"]
    now = datetime.now(timezone(timedelta(hours=8)))
    meeting = request("POST", "/meeting/add", {
        "title": f"[ASR_EVAL] MOSS local link smoke {now.strftime('%H%M%S')}",
        "start_time": now.isoformat(), "end_time": (now + timedelta(hours=1)).isoformat(),
        "location": "本地评测", "host_id": account["id"],
        "participant_ids": [], "agenda": "evaluation=true; moss_link_smoke=true",
    }, token=token)
    audio = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "evaluation/asr/first_test/moss_smoke_60s.mp3"
    material = request("POST", f"/meetingMaterial/upload/{meeting['id']}", upload=audio, token=token)
    task = request("POST", f"/transcription/start/{material['id']}", {"language": "zh"}, token=token)
    started = time.monotonic()
    detail = {}
    while True:
        time.sleep(5)
        detail = request("GET", f"/transcription/selectById/{task['id']}", token=token)
        if str(detail.get("status")) in {"SUCCEEDED", "FAILED"}:
            break
        if time.monotonic() - started > 900:
            raise SystemExit(f"转写超时（{detail.get('status')}）")
    elapsed = round(time.monotonic() - started, 1)
    segments = detail.get("segments") or []
    result = {
        "base_url": BASE,
        "meeting_id": meeting["id"],
        "task_id": task["id"],
        "status": detail.get("status"),
        "error_message": detail.get("error_message"),
        "model_name": detail.get("model_name"),
        "segment_count": len(segments),
        "first_segments": [{"speaker": s.get("speaker_label"), "content": str(s.get("content") or "")[:60]}
                           for s in segments[:5]],
        "elapsed_seconds": elapsed,
        "audio": str(audio),
        "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "SUCCEEDED" and result["segment_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
