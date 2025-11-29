"""离线回放：生成后引用绑定（citation binding）的上界与效果。

零模型调用：对现有 packet 的 answer+contexts 重放 `services.citation_bind.bind_answer`，
再用与 `analysis/claim_citation_audit.py` 完全相同的口径重算数字 attribution。
这是 LEDGER §49 预注册的第一道闸：上界够不够，先看回放；不够不产品化。

用法：
    evaluation/.venv/bin/python evaluation/analysis/evidence_bind_replay.py \
        --out-json evaluation/results/evidence_bind_replay.json \
        --out-md evaluation/reports/2026-10-23_evidence_binding.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from analysis.claim_citation_audit import audit_case  # noqa: E402
from services.citation_bind import bind_answer  # noqa: E402

PACKETS = ("cross30_fresh", "mixed100", "new_meeting",
           "p0_r1", "p0_r2", "p0_r3", "extended68")
PACKET_PATHS = {
    "cross30_fresh": EVAL / "results/cross30_fresh_packet.json",
    "mixed100": EVAL / "results/eval100_scope_fixed_packet.json",
    "new_meeting": EVAL / "results/new_meeting_packet.json",
    "p0_r1": EVAL / "results/p0_fresh6_r1_packet.json",
    "p0_r2": EVAL / "results/p0_fresh6_r2_packet.json",
    "p0_r3": EVAL / "results/p0_fresh6_r3_packet.json",
    "extended68": EVAL / "results/extended68_packet.json",
}
THRESHOLD = {"packet": "cross30_fresh", "min_rebound": 15}
CLAIM = "LEDGER §49：cited-gap 由 39/213（18.31%）向 any-gap 20/213（9.39%）收敛"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    result: dict = {"policy": CLAIM, "packets": {}, "examples": []}
    for name in PACKETS:
        path = PACKET_PATHS[name]
        if not path.exists():
            result["packets"][name] = {"missing": str(path)}
            continue
        packet = json.loads(path.read_text(encoding="utf-8"))
        before = {"numbers_checked": 0, "unsupported_cited": 0, "unsupported_any": 0}
        after = {"numbers_checked": 0, "unsupported_cited": 0, "unsupported_any": 0}
        rebound = unbound = already = checked = uncited = 0
        changed_cases = 0
        for case in packet.get("cases") or []:
            if not case.get("answer"):
                continue
            pre = audit_case(case)
            bind = bind_answer(case["answer"], case.get("contexts") or [])
            rebound += bind.rebound
            unbound += bind.unbound
            already += bind.already_supported
            checked += bind.checked_numbers
            uncited += bind.uncited_numbers
            mutated = dict(case)
            mutated["answer"] = bind.answer
            post = audit_case(mutated)
            if bind.answer != case["answer"]:
                changed_cases += 1
            for key, row in (("before", pre), ("after", post)):
                bucket = before if key == "before" else after
                bucket["numbers_checked"] += row["numbers_checked"]
                bucket["unsupported_cited"] += len(row["unsupported_cited"])
                bucket["unsupported_any"] += len(row["unsupported_any"])
            for row in bind.rebinds[:2]:
                if len(result["examples"]) < 12:
                    result["examples"].append({"packet": name, "case_id": case.get("case_id"), **row})
        total = before["numbers_checked"] or 1
        total_after = after["numbers_checked"] or 1
        result["packets"][name] = {
            "cases": len(packet.get("cases") or []),
            "changed_cases": changed_cases,
            "checked_numbers": checked,
            "already_supported": already,
            "rebound": rebound,
            "unbound": unbound,
            "uncited_numbers": uncited,
            "before": {
                "numbers_checked": before["numbers_checked"],
                "unsupported_cited": before["unsupported_cited"],
                "cited_gap_rate": round(before["unsupported_cited"] / total, 4),
                "unsupported_any": before["unsupported_any"],
                "not_in_any_rate": round(before["unsupported_any"] / total, 4),
            },
            "after": {
                "numbers_checked": after["numbers_checked"],
                "unsupported_cited": after["unsupported_cited"],
                "cited_gap_rate": round(after["unsupported_cited"] / total_after, 4),
                "unsupported_any": after["unsupported_any"],
                "not_in_any_rate": round(after["unsupported_any"] / total_after, 4),
            },
            "reduction_pp": round((before["unsupported_cited"] - after["unsupported_cited"]) / total * 100, 2),
        }
    cross = result["packets"].get("cross30_fresh") or {}
    result["s49_gate"] = {
        "packet": "cross30_fresh",
        "rebound": cross.get("rebound"),
        "unsupported_any": (cross.get("after") or {}).get("unsupported_any"),
        "min_rebound": THRESHOLD["min_rebound"],
        "pass": (cross.get("rebound") or 0) >= THRESHOLD["min_rebound"],
        "reading": ("数字 cited-gap 的理论下界是 any-gap：能重绑的都已绑上后，"
                    "cited-gap 应等于 not_in_any（重绑不改变 any-gap）。"),
    }
    result["quote_note"] = "引语不在本脚本范围：quality_gap_report 已显示 cross30 引语 13/13 不在任何上下文，不可重绑。"
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 生成后引用绑定：离线回放（2026-10-23，零模型调用）",
        "",
        "> 预注册：`evaluation/LEDGER.md §49`。对现有 packet 的答案重放 `services/citation_bind.bind_answer`，",
        "> 用同一套 `claim_citation_audit` 口径重算数字 attribution。**回放通过才产品化。**",
        "",
        "## 结论",
        "",
        f"- cross30 重绑 **{cross.get('rebound')}** 条数字（门槛 ≥{THRESHOLD['min_rebound']}）→ "
        f"**{'PASS' if (cross.get('rebound') or 0) >= THRESHOLD['min_rebound'] else 'FAIL'}**",
        f"- cross30 cited-gap：{(cross.get('before') or {}).get('cited_gap_rate')} → "
        f"{(cross.get('after') or {}).get('cited_gap_rate')}（−{cross.get('reduction_pp')}pp）；",
        f"  any-gap 不变（{((cross.get('before') or {}).get('not_in_any_rate'))} → "
        f"{((cross.get('after') or {}).get('not_in_any_rate'))}）——重绑不改变'哪里都没有'。",
        "- 引语（cross30 13/13 不在任何上下文）不可重绑，本机制不处理。",
        "",
        "## 逐 packet",
        "",
        "| packet | 检查数字 | 重绑 | unbound | cited-gap 前 | cited-gap 后 | 降幅(pp) | any-gap 前后 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in PACKETS:
        row = result["packets"].get(name) or {}
        if row.get("missing"):
            lines.append(f"| {name} | 缺产物 | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {name} | {row['before']['numbers_checked']} | {row['rebound']} | {row['unbound']} | "
            f"{row['before']['unsupported_cited']} ({row['before']['cited_gap_rate']:.2%}) | "
            f"{row['after']['unsupported_cited']} ({row['after']['cited_gap_rate']:.2%}) | "
            f"{row['reduction_pp']:+.2f} | "
            f"{row['before']['not_in_any_rate']:.2%}→{row['after']['not_in_any_rate']:.2%} |")
    lines += [
        "",
        "## 限制",
        "",
        "- 字面匹配口径（与审计一致）：数字写法不同（中文数字/日期格式差异）不计入重绑；",
        "- 只重绑、不删内容：`unbound`（真正不在任何上下文）保持原样，留给后续'降级表述'策略；",
        "- 回放用的是现有答案与上下文（不再调模型）；真实运行时还需 A/B 验证（§49）。",
        "",
    ]
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({name: {
        "checked": (row.get("before") or {}).get("numbers_checked"),
        "rebound": row.get("rebound"),
        "cited_gap_before": (row.get("before") or {}).get("cited_gap_rate"),
        "cited_gap_after": (row.get("after") or {}).get("cited_gap_rate"),
        "any_gap": (row.get("after") or {}).get("not_in_any_rate"),
    } for name, row in result["packets"].items() if row.get("before")}, ensure_ascii=False, indent=2))
    print("S49:", "PASS" if result["s49_gate"]["pass"] else "FAIL")
    print(f"→ {args.out_json}\n→ {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
