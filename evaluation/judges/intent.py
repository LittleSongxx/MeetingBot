"""抓取产品对 100+ 道题的**查询规划决策**，供逐类 F1 评分（每题一次廉价调用）。

## 为什么单开一个入口

意图 F1 只需要"产品怎么判"这一个信号。走完整问答链路（`judges/rag.py --ask`）每题要
2–4 次调用、还要检索与生成；而 `POST /chat/plan` 只跑一次规划就返回。100 题按这个方法
是量级上的差别（约 100 次廉价调用 vs 300+ 次含生成的调用）。

**零调用部分在评分器里**：`regression/intent_metrics.py` 只读本脚本产出的
`results/intent_decisions.json`，不再打模型——所以这一节可以每次 `make smoke` 复查。

## 产物与留痕

- `decisions`：每题一行 `{case_id, question, intent, rewritten, suggested_tools, triggers}`。
- `captured_from`：base_url、抓取时间、**产品自报的开关状态**（打 `/chat/status` 读
  `kb_query_plan_enabled` / `kb_rerank_enabled`）。抓取进程的 env 与容器的 env 是两个空间，
  开关必须由被测进程自己报——`product_model` 留空已经演示过"从外面猜"的后果。
- 失败如实计数：某一题超时/报错就记 `error`，**不静默丢**，评分器会把它列进 skipped。

## 预算

`--authorized-llm-calls` 是硬预算闸（每题 1 次规划调用，超了拒绝执行）——
与本仓"付费运行先预注册、预算显式"的纪律一致。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EVALUATION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = EVALUATION_DIR / "results"
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"
DEFAULT_OUTPUT = RESULTS_DIR / "intent_decisions.json"


def _http_json(url: str, *, method: str = "POST", payload: dict | None = None,
               token: str = "", timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json;charset=utf-8")
    request.add_header("token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自命令行
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"{method} {url} → HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"{method} {url} → {error.reason}") from error


def product_switches(base_url: str, token: str) -> dict[str, Any]:
    """被测进程自报的开关状态（读不到就如实写 unknown，不猜）。"""
    try:
        status = _http_json(f"{base_url}/chat/status", method="GET", token=token)
    except RuntimeError as error:
        return {"unavailable": str(error)[:200]}
    data = (status or {}).get("data") or {}
    return {
        "model": data.get("model"),
        "kb_rerank_enabled": data.get("kb_rerank_enabled"),
        "kb_query_plan_enabled": data.get("kb_query_plan_enabled"),
    }


def capture_decisions(qa_set: Mapping[str, Any], *, base_url: str, token: str,
                      output_path: Path, progress=print) -> dict[str, Any]:
    switches = product_switches(base_url, token)
    if not switches.get("kb_query_plan_enabled"):
        # 不拒绝执行（端点自己也会回 enabled=false 的空规划），但必须显眼地提示：
        # 关着开关跑出来的产物只能证明"开关是关的"，量不了意图分类
        progress(f"⚠ 产品自报 kb_query_plan_enabled={switches.get('kb_query_plan_enabled')}："
                 "规划没开时产物无法用于意图 F1")
    decisions: list[dict[str, Any]] = []
    for case in qa_set["cases"]:
        row: dict[str, Any] = {"case_id": case["id"], "question": case["question"]}
        try:
            reply = _http_json(f"{base_url}/chat/plan", payload={"question": case["question"]},
                               token=token)
            data = (reply or {}).get("data") or {}
            plan = data.get("plan") or {}
            row.update({
                "enabled": data.get("enabled"),
                "used": plan.get("used"),
                "intent": plan.get("intent"),
                "rewritten": plan.get("rewritten"),
                "suggested_tools": plan.get("suggested_tools"),
                "triggers": plan.get("triggers"),
                "reason": plan.get("reason"),
            })
        except RuntimeError as error:
            row["error"] = str(error)[:300]
        decisions.append(row)
        progress(f"  [{case['id']}] {case['question'][:24]}… → {row.get('intent') or row.get('reason') or '失败'}")
        time.sleep(0.1)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(_artifact(base_url, switches, decisions),
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    return {"cases": len(decisions),
            "decided": sum(1 for row in decisions if row.get("intent")),
            "failed": sum(1 for row in decisions if row.get("error"))}


def _artifact(base_url: str, switches: Mapping[str, Any],
              decisions: list[dict[str, Any]]) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {
        "artifact_version": 1,
        "captured_from": {
            "base_url": base_url,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "product_switches": dict(switches),
        },
        "decisions": decisions,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--token", default="")
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--authorized-llm-calls", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
    if not args.authorized_llm_calls:
        print("拒绝执行：每题一次规划调用（真实模型调用），必须显式传 --authorized-llm-calls",
              file=sys.stderr)
        return 2
    if args.authorized_llm_calls < len(qa_set["cases"]):
        print(f"拒绝执行：授权 {args.authorized_llm_calls} 次 < 题目数 {len(qa_set['cases'])}"
              "（每题一次，凑不够就别跑，免得产物半截）", file=sys.stderr)
        return 2
    if not args.token:
        print("拒绝执行：--token 是登录后的 JWT", file=sys.stderr)
        return 2
    result = capture_decisions(qa_set, base_url=args.base_url, token=args.token,
                               output_path=args.output)
    print(json.dumps(result, ensure_ascii=False))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
