"""工具调用效用审计：区分“多余且无贡献”的 over-call 与“多调但贡献了引用”的 over-call。

零模型调用；用 citation kind 映射工具类别，仅作代理指标。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TOOL_KIND = {
    "search_knowledge": {"chunk"},
    "get_transcript_excerpt": {"segment"},
    "list_facts": {"fact"},
    "get_minutes": {"fact"},
    "query_meeting_state": {"state"},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    by_question = {turn["question"]: turn for turn in artifact.get("turns") or []}
    rows = []
    extras = unused = 0
    for case in report.get("per_case") or []:
        turn = by_question.get(case.get("question"))
        if not turn:
            continue
        tools = {item.get("name") for item in turn.get("tools") or []}
        required = set((case.get("first_trial") or {}).get("required_tools") or [])
        extra = tools - required
        if not extra:
            continue
        kinds = {item.get("kind") for item in turn.get("citations") or []}
        no_contrib = sorted(tool for tool in extra
                            if not (TOOL_KIND.get(tool, set()) & kinds))
        extras += len(extra)
        unused += len(no_contrib)
        rows.append({"case_id": case["case_id"], "used": sorted(tools),
                     "required": sorted(required), "extra": sorted(extra),
                     "citation_kinds": sorted(k for k in kinds if k),
                     "extra_without_citation_kind": no_contrib})
    result = {"artifact": str(args.artifact), "report": str(args.report),
              "extra_calls": extras, "extra_without_citation_kind": unused,
              "rows": rows}
    print(json.dumps({k: result[k] for k in ("extra_calls", "extra_without_citation_kind")},
                     ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
