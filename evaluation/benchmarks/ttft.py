"""RAG 链路 TTFT / 首 token / 端到端延迟（SSE 客户端侧实测，只走产品 HTTP）。

口径：
- TTFT = 发请求 → 收到第一个 `event: token`（含检索、工具、模型首 token）；
- first_event = 发请求 → 第一个 SSE 事件（meta 等，不含生成）；
- done = 发请求 → `event: done`（服务端结束）；
- 每题新建 session，避免历史长度影响。
用法：
    evaluation/.venv/bin/python evaluation/benchmarks/ttft.py --limit 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = REPO / "meetingbot/fastapi-app/.env"


def load_env() -> dict[str, str]:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def http_json(base: str, path: str, token: str, payload=None, method="GET") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "token": token}
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return ordered[index]


def stream_once(base: str, token: str, session_id: int, question: str) -> dict:
    request = urllib.request.Request(
        f"{base}/chat/stream",
        data=json.dumps({"session_id": session_id, "question": question}).encode("utf-8"),
        method="POST")
    request.add_header("Content-Type", "application/json;charset=utf-8")
    request.add_header("token", token)
    started = time.perf_counter()
    first_event = first_token = done = None
    events: list[str] = []
    tool_calls = 0
    with urllib.request.urlopen(request, timeout=300) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line.startswith("event:"):
                continue
            event = line.split(":", 1)[1].strip()
            elapsed = (time.perf_counter() - started) * 1000
            if first_event is None:
                first_event = elapsed
            events.append(event)
            if event == "token" and first_token is None:
                first_token = elapsed
            elif event == "tool_end":
                tool_calls += 1
            elif event in {"done", "error"}:
                done = elapsed
                break
    total = done if done is not None else (time.perf_counter() - started) * 1000
    return {"first_event_ms": first_event, "ttft_ms": first_token,
            "done_ms": total, "tool_calls": tool_calls,
            "event_count": len(events)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--qa-set", type=Path,
                        default=REPO / "evaluation/results/qa_new_meeting_15.json")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path,
                        default=REPO / "evaluation/results/ttft_report.json")
    args = parser.parse_args()
    env = load_env()
    login = http_json(args.base_url, "/login", "", {
        "username": env["APP_ADMIN_USERNAME"], "password": env["APP_ADMIN_PASSWORD"]},
        method="POST")
    token = (login.get("data") or {}).get("token") or login.get("token")
    qa = json.loads(args.qa_set.read_text(encoding="utf-8"))
    cases = qa["cases"][:args.limit]
    rows = []
    for case in cases:
        created = http_json(args.base_url, "/chat/session", token,
                            {"meeting_ids": case.get("meeting_ids"),
                             "series_ids": case.get("series_ids")}, method="POST")
        session_id = (created.get("data") or {}).get("id")
        if not session_id:
            raise RuntimeError(f"建会话失败：{created}")
        result = stream_once(args.base_url, token, session_id, case["question"])
        history = http_json(args.base_url,
                            f"/chat/messages?session_id={session_id}&limit=10", token)
        rows_data = (history.get("data") or {}).get("list") or []
        assistant = next((row for row in reversed(rows_data)
                          if row.get("role") == "ASSISTANT"), {})
        row = {"case_id": case["id"], "question": case["question"],
               "message_id": assistant.get("id"), **result}
        rows.append(row)
        print(f"[{case['id']}] TTFT={result['ttft_ms']:.0f}ms "
              f"done={result['done_ms']:.0f}ms tools={result['tool_calls']}", flush=True)
    ttfts = [row["ttft_ms"] for row in rows if row["ttft_ms"] is not None]
    dones = [row["done_ms"] for row in rows]
    firsts = [row["first_event_ms"] for row in rows if row["first_event_ms"] is not None]
    summary = {
        "n": len(rows),
        "ttft_ms": {"mean": round(statistics.mean(ttfts), 1) if ttfts else None,
                    "p50": round(percentile(ttfts, 0.50), 1) if ttfts else None,
                    "p95": round(percentile(ttfts, 0.95), 1) if ttfts else None,
                    "max": round(max(ttfts), 1) if ttfts else None},
        "first_event_ms": {"p50": round(percentile(firsts, 0.50), 1) if firsts else None,
                           "p95": round(percentile(firsts, 0.95), 1) if firsts else None},
        "done_ms": {"mean": round(statistics.mean(dones), 1),
                    "p50": round(percentile(dones, 0.50), 1),
                    "p95": round(percentile(dones, 0.95), 1),
                    "max": round(max(dones), 1)},
        "mean_tool_calls": round(statistics.mean(row["tool_calls"] for row in rows), 2),
    }
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "base_url": args.base_url, "qa_set": str(args.qa_set),
               "summary": summary, "rows": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("→", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
