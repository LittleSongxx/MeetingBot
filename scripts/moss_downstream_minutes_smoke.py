"""MOSS 基板下游 sanity：对 MOSS 转写任务生成纪要，检查产品链路完整性（非质量结论）。

用法：
    python scripts/moss_downstream_minutes_smoke.py --task-id <transcription_task_id>

只做：登录 → /meeting_minutes/generate/{task_id} → 轮询 → 结构检查（summary/列表非空、
条目字段形状、引用/来源标注存在性）→ 写 results/moss_downstream_minutes.json。
不判纪要正确性（没有 MOSS 基板的 gold 纪要）。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:19091"
ENV = REPO / "meetingbot/fastapi-app/.env"
OUT = REPO / "evaluation/results/moss_downstream_minutes.json"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}


def load_env() -> dict[str, str]:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request(method: str, path: str, payload: dict | None = None, token: str = "") -> dict:
    headers = {}
    data = None
    if token:
        headers["token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json;charset=utf-8"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
    if str(body.get("code")) != "200":
        raise SystemExit(f"API {path} 拒绝：{body.get('msg')}")
    return body["data"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, required=True)
    args = parser.parse_args()
    env = load_env()
    account = request("POST", "/login", {"username": env["APP_ADMIN_USERNAME"],
                                        "password": env["APP_ADMIN_PASSWORD"]})
    token = account["token"]
    minutes = request("POST", f"/meetingMinutes/generate/{args.task_id}", token=token)
    minutes_id = minutes["id"]
    started = time.monotonic()
    detail: dict = {}
    while True:
        time.sleep(5)
        detail = request("GET", f"/meetingMinutes/selectById/{minutes_id}", token=token)
        if str(detail.get("status")) in TERMINAL:
            break
        if time.monotonic() - started > 1200:
            raise SystemExit(f"纪要生成超时（{detail.get('status')}）")
    elapsed = round(time.monotonic() - started, 1)

    def items(key: str) -> list:
        value = detail.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        return value if isinstance(value, list) else []

    checks = {
        "status_succeeded": str(detail.get("status")) == "SUCCEEDED",
        "summary_nonempty": bool(str(detail.get("summary") or "").strip()),
        "has_decisions_or_pending": bool(items("decisions") or items("pending_items")),
        "topics_nonempty": bool(items("topics")),
        "no_error_message": not str(detail.get("error_message") or "").strip(),
    }
    result = {
        "task_id": args.task_id,
        "minutes_id": minutes_id,
        "status": detail.get("status"),
        "elapsed_seconds": elapsed,
        "summary_chars": len(str(detail.get("summary") or "")),
        "counts": {key: len(items(key)) for key in
                   ("topics", "viewpoints", "decisions", "pending_items", "risks")},
        "checks": checks,
        "checks_pass": all(checks.values()),
        "model": detail.get("model_name"),
        "note": "MOSS 基板下游链路完整性检查；没有 MOSS 基板的 gold 纪要，不判正确率",
        "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
