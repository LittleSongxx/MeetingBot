"""S1 A/B 分析：绑定开/关两臂的 attribution + 四指标 + 差异。

零模型调用（两臂 packet 已判定）。判定线见 LEDGER §49。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))

from analysis.claim_citation_audit import audit_case  # noqa: E402

OFF = EVAL / "results/cross30_bindoff_packet.json"
ON = EVAL / "results/cross30_bindon_packet.json"
OUT = EVAL / "results/evidence_binding_ab.json"
MD = EVAL / "reports/2026-10-23_evidence_binding.md"


def audit(packet: dict) -> dict:
    checked = bad_cited = bad_any = 0

    for case in packet.get("cases") or []:
        row = audit_case(case)
        checked += row["numbers_checked"]
        bad_cited += len(row["unsupported_cited"])
        bad_any += len(row["unsupported_any"])
    return {"numbers_checked": checked, "unsupported_cited": bad_cited,
            "unsupported_any": bad_any,
            "cited_gap_rate": round(bad_cited / checked, 4) if checked else None,
            "not_in_any_rate": round(bad_any / checked, 4) if checked else None}


def _rate(value):
    if isinstance(value, dict):
        return value.get("rate")
    return value


def metrics(packet: dict) -> dict:
    summary = (packet.get("summary") or {})
    return {key: _rate(summary.get(key)) for key in (
        "context_precision", "context_recall", "faithfulness",
        "faithfulness_content", "answer_relevancy")}


def main() -> int:
    off_packet = json.loads(OFF.read_text(encoding="utf-8"))
    on_packet = json.loads(ON.read_text(encoding="utf-8"))
    off, on = audit(off_packet), audit(on_packet)
    off_m, on_m = metrics(off_packet), metrics(on_packet)
    answers_off = {case["case_id"]: case.get("answer") or "" for case in off_packet["cases"]}
    answers_on = {case["case_id"]: case.get("answer") or "" for case in on_packet["cases"]}
    changed_cases = [cid for cid in answers_off if answers_off[cid] != answers_on.get(cid)]
    result = {
        "policy": "LEDGER §49：主指标 cited-gap；faithfulness 非劣 ≤2pp；relevancy 非劣；any-gap 不变",
        "arms": {"off": {"attribution": off, "metrics": off_m},
                 "on": {"attribution": on, "metrics": on_m}},
        "delta": {
            "cited_gap_pp": round(((on["cited_gap_rate"] or 0) - (off["cited_gap_rate"] or 0)) * 100, 2),
            "any_gap_pp": round(((on["not_in_any_rate"] or 0) - (off["not_in_any_rate"] or 0)) * 100, 2),
            **{key: round(((on_m.get(key) or 0) - (off_m.get(key) or 0)) * 100, 2)
               if isinstance(off_m.get(key), (int, float)) else None
               for key in off_m},
        },
        "changed_cases": changed_cases,
        "changed_case_count": len(changed_cases),
        "s49_checks": {
            "cited_gap_abs_ge_5pp_or_rel_30pct": bool(
                (off["cited_gap_rate"] or 0) - (on["cited_gap_rate"] or 0) >= 0.05
                or ((off["cited_gap_rate"] or 0) - (on["cited_gap_rate"] or 0)) / (off["cited_gap_rate"] or 1) >= 0.30),
            "faithfulness_non_inferior": bool(
                (on_m.get("faithfulness_content") or 0) >= (off_m.get("faithfulness_content") or 0) - 0.02),
            "relevancy_non_inferior": bool(
                (on_m.get("answer_relevancy") or 0) >= (off_m.get("answer_relevancy") or 0) - 0.02),
            "any_gap_unchanged": abs((on["not_in_any_rate"] or 0) - (off["not_in_any_rate"] or 0)) <= 0.01,
        },
        "limitations": [
            "单裁判（deepseek-flash，非人工金标）",
            "cross30 30 题单次运行（单次波动约 5pp，本实验只看引用规则变化）",
            "绑定开关通过 .env 切换；两臂同模型/同检索配置",
        ],
    }
    result["s49_pass"] = all(result["s49_checks"].values())
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 生成后引用绑定：离线回放 + 运行时 A/B（2026-10-23）",
        "",
        "> 预注册：`evaluation/LEDGER.md §49`。离线回放先看上界，通过后实现运行时绑定并做 30 题 A/B。",
        "",
        "## 1. 离线回放（零调用，已有答案重放）",
        "",
        "| packet | 检查数字 | 重绑 | cited-gap 前→后 | any-gap 前后 |",
        "|---|---:|---:|---|---|",
    ]
    replay_path = EVAL / "results/evidence_bind_replay.json"
    if replay_path.exists():
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        for name in ("cross30_fresh", "mixed100", "new_meeting", "p0_r1", "p0_r2", "p0_r3"):
            row = (replay.get("packets") or {}).get(name) or {}
            if "before" not in row:
                continue
            lines.append(
                f"| {name} | {row['before']['numbers_checked']} | {row['rebound']} | "
                f"{row['before']['cited_gap_rate']:.2%}→{row['after']['cited_gap_rate']:.2%} | "
                f"{row['before']['not_in_any_rate']:.2%}→{row['after']['not_in_any_rate']:.2%} |")
        lines += ["", f"回放闸：cross30 重绑 {replay.get('s49_gate', {}).get('rebound')} 条 "
                       f"（门槛 ≥{replay.get('s49_gate', {}).get('min_rebound')}）→ "
                       f"**{'PASS' if replay.get('s49_gate', {}).get('pass') else 'FAIL'}**"]
    lines += [
        "",
        "## 2. 运行时 A/B（30 题 cross30 fresh，off/on）",
        "",
        "| 指标 | OFF | ON | 差值(pp) |",
        "|---|---:|---:|---:|",
        f"| 数字 cited-gap | {off['cited_gap_rate']:.2%} | {on['cited_gap_rate']:.2%} | "
        f"{result['delta']['cited_gap_pp']:+.2f} |",
        f"| 数字 any-gap | {off['not_in_any_rate']:.2%} | {on['not_in_any_rate']:.2%} | "
        f"{result['delta']['any_gap_pp']:+.2f} |",
        f"| context_precision | {off_m.get('context_precision'):.3f} | {on_m.get('context_precision'):.3f} | "
        f"{result['delta'].get('context_precision', 0):+.2f} |",
        f"| context_recall | {off_m.get('context_recall'):.3f} | {on_m.get('context_recall'):.3f} | "
        f"{result['delta'].get('context_recall', 0):+.2f} |",
        f"| faithfulness(内容级) | {off_m.get('faithfulness_content'):.3f} | "
        f"{on_m.get('faithfulness_content'):.3f} | "
        f"{result['delta'].get('faithfulness_content', 0):+.2f} |",
        f"| answer_relevancy | {off_m.get('answer_relevancy'):.3f} | {on_m.get('answer_relevancy'):.3f} | "
        f"{result['delta'].get('answer_relevancy', 0):+.2f} |",
        "",
        f"- 绑定改变了 **{len(changed_cases)}/30** 题答案文本",
        "",
        "## 3. §49 判定",
        "",
        f"- ① cited-gap 绝对 ≥5pp 或相对 ≥30%：{result['s49_checks']['cited_gap_abs_ge_5pp_or_rel_30pct']}",
        f"- ② faithfulness 非劣（−2pp）：{result['s49_checks']['faithfulness_non_inferior']}",
        f"- ③ relevancy 非劣：{result['s49_checks']['relevancy_non_inferior']}",
        f"- ④ any-gap 不变（±1pp）：{result['s49_checks']['any_gap_unchanged']}",
        f"- **总判定：{'PASS → 默认开' if result['s49_pass'] else 'FAIL → 默认关'}**",
        "",
        "## 4. 限制",
        "",
        "- 单裁判、单次运行（cross30 单次波动约 5pp）；本实验的 cited-gap 是规则审计，不是语义真值；",
        "- 引语 13/13 不在任何上下文，本机制不处理；unbound 数字保持原样（不删信息）；",
        "- 绑定只加引用，不改答案意思；faithfulness 由 judge 重判，属方向信号。",
        "",
    ]
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"off": off, "on": on, "delta": result["delta"],
                      "changed": len(changed_cases), "s49_pass": result["s49_pass"]},
                     ensure_ascii=False, indent=2))
    print(f"→ {OUT}\n→ {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
