"""P3 下一步：对 claim/citation 审计标出的数字缺口做 LLM claim 校验。

只做分类，不改产品；用于区分 supported_by_cited / supported_by_other_context /
derived / citation_mapping / unsupported。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import model_client  # noqa: E402

SYSTEM = """你是 RAG 答案的 claim/citation 校验器。给定问题、答案句、被引上下文和本轮全部上下文，
判断答案句里的一个数字/日期是否被支持。输出 JSON：
{"status":"supported_by_cited|supported_by_other_context|derived|citation_mapping|unsupported",
 "reason":"一句话，必须引用上下文证据或说明派生方式"}
判定口径：
- supported_by_cited：被引上下文直接写出该数字/日期；
- supported_by_other_context：被引上下文没有，但本轮其他上下文有；
- derived：由上下文中的数字/日期计算、换算、合并得到（说明算式/来源）；
- citation_mapping：数字其实是引用编号/时间定位/会议日期元数据，不是事实断言；
- unsupported：以上都不是。
不要用外部知识；不确定时选 unsupported 并说明。"""


def env() -> dict[str, str]:
    values = {}
    for line in (EVAL.parent / "meetingbot/fastapi-app/.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    os.environ.update({key: value for key, value in values.items() if key not in os.environ})
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in packet["cases"]}
    env()
    url, model, key = model_client.endpoint("deepseek")
    results = []
    for row in audit["rows"]:
        case = cases.get(row["case_id"]) or {}
        contexts = case.get("contexts") or []
        by_ref = {int(item.get("ref")): item for item in contexts if item.get("ref") is not None}
        for gap in row["unsupported_cited"]:
            cited = [by_ref.get(ref) or {} for ref in gap["refs"]]
            cited_text = "\n".join(str(item.get("text") or "")[:1200] for item in cited if item)
            all_text = "\n".join(str(item.get("text") or "")[:800] for item in contexts)
            prompt = (f"【问题】\n{case.get('question') or ''}\n\n"
                      f"【答案句】\n{gap['sentence']}\n\n"
                      f"【待核对的数字/日期】\n{gap['token']}\n\n"
                      f"【被引上下文】\n{cited_text or '（空）'}\n\n"
                      f"【本轮全部上下文】\n{all_text or '（空）'}\n\n请输出 JSON。")
            try:
                parsed = model_client.call_json(url, model, key, SYSTEM, prompt,
                                                timeout=120, total_deadline=240, thinking=False)
                parsed = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else parsed
                status = parsed.get("status") if isinstance(parsed, dict) else None
                reason = parsed.get("reason") if isinstance(parsed, dict) else ""
            except Exception as error:  # noqa: BLE001
                status, reason = "error", str(error)[:200]
            results.append({"case_id": row["case_id"], "token": gap["token"],
                            "refs": gap["refs"], "sentence": gap["sentence"],
                            "status": status or "error", "reason": reason})
            print(f"[{len(results)}] {row['case_id']} {gap['token']} -> {status}", flush=True)
    summary: dict[str, int] = {}
    for result in results:
        summary[result["status"]] = summary.get(result["status"], 0) + 1
    args.out.write_text(json.dumps({"audit": str(args.audit), "packet": str(args.packet),
                                    "summary": summary, "results": results},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
