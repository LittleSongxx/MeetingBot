"""延迟/成本基准：只用 HTTP 接口，不 import 产品内部，可在测试环境重复跑。

用法：
    python evaluation/benchmarks/latency_cost.py --searches 10 --out evaluation/results/latency_cost_report.json
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
QUERIES = ["发布范围", "灰度回滚", "报表导出", "附件修复", "首周复盘",
           "通稿时间", "值班排班", "风险清单", "补充验收", "专项评审"]


def load_env() -> dict[str, str]:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request(base: str, method: str, path: str, token: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["token"] = token
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode())


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--searches", type=int, default=10)
    parser.add_argument("--out", type=Path, default=REPO / "evaluation/results/latency_cost_report.json")
    args = parser.parse_args()
    env = load_env()
    login = request(args.base_url, "POST", "/login", "", {
        "username": env["APP_ADMIN_USERNAME"], "password": env["APP_ADMIN_PASSWORD"]})
    token = (login.get("data") or {}).get("token") or login.get("token")
    overview = request(args.base_url, "GET", "/observability/overview", token)["data"]
    latencies = []
    hits = []
    for index in range(args.searches):
        query = QUERIES[index % len(QUERIES)]
        started = time.perf_counter()
        data = request(args.base_url, "POST", "/kb/search", token, {"query": query, "limit": 3})["data"]
        latencies.append((time.perf_counter() - started) * 1000)
        hits.append(len(data.get("hits") or []))
    summary = overview["summary"]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overview": {"call_count": summary["call_count"],
                     "average_elapsed_ms": summary["average_elapsed_ms"],
                     "total_tokens": summary["total_tokens"],
                     "cost_micro": summary["cost_micro"],
                     "unpriced_call_count": summary["unpriced_call_count"]},
        "call_types": overview["call_types"],
        "search": {"n": len(latencies), "mean_ms": statistics.mean(latencies),
                   "p95_ms": percentile(latencies, 0.95), "max_ms": max(latencies),
                   "mean_hits": statistics.mean(hits)},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("overview", "search")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
