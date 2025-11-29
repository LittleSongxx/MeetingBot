"""claim 锚点标注汇总：A/B 一致率、Cohen κ、Krippendorff α、与 LLM judge 的对照。

预注册见 evaluation/LEDGER.md §44。零模型调用。
用法：
    python3 evaluation/gold/claim_anchor_score.py \
        --a evaluation/gold/claim_anchor_cross30_100.a.csv \
        --b evaluation/gold/claim_anchor_cross30_100.b.csv \
        --packet evaluation/results/cross30_fresh_packet.json \
        --out-json evaluation/results/claim_anchor_scores.json \
        --out-md evaluation/reports/2026-10-22_claim_anchor_calibration.md
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

EVAL = Path(__file__).resolve().parents[1]
LABELS = ("supported", "unsupported", "uncertain")
KAPPA_THRESHOLD = 0.70  # α 目标，见 §44；同时报 Cohen κ 作对照


def _load(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["sample_id"]: row for row in csv.DictReader(handle)}


def _norm(text: str) -> str:
    text = re.sub(r"\[[0-9]{1,3}\]", "", text or "")
    text = re.sub(r"[*#>\-—\s，,。．.;；:：\"'“”‘’（）()【】\[\]]", "", text)
    return text.strip().lower()


def _norm_label(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in LABELS:
        return text
    if text in ("支持", "支撑", "supported"):
        return "supported"
    if text in ("不支持", "无依据", "unsupported"):
        return "unsupported"
    if text in ("无法判断", "判不了", "uncertain"):
        return "uncertain"
    return ""


def krippendorff_alpha_nominal(rows: list[tuple[str, str]]) -> float | None:
    """2 标注者、nominal、无缺值的简化 Krippendorff α。"""
    pairs = [(a, b) for a, b in rows if a and b]
    if not pairs:
        return None
    n = len(pairs)
    observed_disagreement = sum(1 for a, b in pairs if a != b) / n
    totals = Counter()
    for a, b in pairs:
        totals[a] += 1
        totals[b] += 1
    total = 2 * n
    expected_disagreement = 1.0 - sum((count / total) ** 2 for count in totals.values())
    if expected_disagreement <= 0:
        return None
    return 1.0 - observed_disagreement / expected_disagreement


def cohen_kappa(rows: list[tuple[str, str]]) -> float | None:
    pairs = [(a, b) for a, b in rows if a and b]
    if not pairs:
        return None
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = sum((ca[label] / n) * (cb[label] / n) for label in LABELS)
    if pe >= 1:
        return None
    return (po - pe) / (1 - pe)


def _packet_claims(packet: Mapping[str, Any]) -> list[dict]:
    rows = []
    for case in packet.get("cases") or []:
        judgement = case.get("judgement") or {}
        answer = judgement.get("answer") or {}
        for claim in answer.get("claims") or []:
            if not isinstance(claim, Mapping) or "supported" not in claim:
                continue
            rows.append({
                "case_id": case.get("case_id"),
                "text": str(claim.get("text") or ""),
                "norm": _norm(str(claim.get("text") or "")),
                "supported": bool(claim.get("supported")),
                "meta": bool(claim.get("meta")),
            })
    return rows


def match_judge(row: Mapping[str, str], claims: list[dict]) -> dict | None:
    """在 packet 里按 case_id + 文本相似度找该 claim 的 judge 标签。"""
    case_id = row.get("case_id")
    norm = _norm(row.get("claim_text") or "")
    candidates = [claim for claim in claims if claim["case_id"] == case_id and not claim["meta"]]
    best = None
    best_score = 0.0
    for claim in candidates:
        other = claim["norm"]
        if not other or not norm:
            continue
        if other == norm:
            return claim
        if norm in other or other in norm:
            score = min(len(norm), len(other)) / max(len(norm), len(other))
        else:
            # 前缀匹配（CSV 里 claim 被截断到 1200 字符时仍可对上）
            prefix = min(len(norm), len(other), 40)
            score = 0.0
            for size in range(prefix, 11, -2):
                if norm[:size] == other[:size]:
                    score = size / max(len(norm), len(other))
                    break
        if score > best_score:
            best_score, best = score, claim
    return best if best_score >= 0.6 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a", type=Path, required=True)
    parser.add_argument("--b", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    a_rows, b_rows = _load(args.a), _load(args.b)
    sample_ids = sorted(set(a_rows) & set(b_rows))
    pairs: list[tuple[str, str]] = []
    rows = []
    missing = []
    for sample_id in sample_ids:
        row_a = a_rows[sample_id]
        row_b = b_rows[sample_id]
        label_a = _norm_label(row_a.get("annotator_A_verdict"))
        label_b = _norm_label(row_b.get("annotator_B_verdict"))
        if not label_a or not label_b:
            missing.append(sample_id)
        pairs.append((label_a, label_b))
        rows.append({"sample_id": sample_id, "case_id": row_a.get("case_id"),
                     "claim_text": row_a.get("claim_text") or row_a.get("claim") or "",
                     "a": label_a, "b": label_b,
                     "a_note": row_a.get("annotator_A_note") or "",
                     "b_note": row_b.get("annotator_B_note") or ""})
    valid = [(a, b) for a, b in pairs if a and b]
    agreement = sum(1 for a, b in valid if a == b) / len(valid) if valid else None
    consensus = [row for row in rows if row["a"] and row["a"] == row["b"] and row["a"] != "uncertain"]
    disagreements = [row for row in rows if row["a"] and row["b"] and row["a"] != row["b"]]

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    claims = _packet_claims(packet)
    judge_rows = []
    unmatched = 0
    for row in consensus:
        claim = match_judge(row, claims)
        if claim is None:
            unmatched += 1
            continue
        row["judge_supported"] = claim["supported"]
        judge_rows.append(row)
    confusion: Counter[tuple[str, bool]] = Counter()
    for row in judge_rows:
        confusion[(row["a"], row["judge_supported"])] += 1
    judge_accuracy = (sum(count for (label, supported), count in confusion.items()
                          if (label == "supported") == supported) / len(judge_rows)
                      if judge_rows else None)
    unsupported_judge = confusion.get(("unsupported", True), 0) + confusion.get(("unsupported", False), 0)
    judge_yes_rate_given_unsupported = (
        confusion.get(("unsupported", True), 0) / unsupported_judge if unsupported_judge else None)

    result = {
        "pre_registration": "evaluation/LEDGER.md §44",
        "n_a": len(a_rows), "n_b": len(b_rows), "n_pairs": len(valid),
        "missing_verdicts": missing,
        "label_distribution": {
            "A": dict(Counter(row["a"] for row in rows if row["a"])),
            "B": dict(Counter(row["b"] for row in rows if row["b"])),
        },
        "raw_agreement": agreement,
        "cohen_kappa": cohen_kappa(valid),
        "krippendorff_alpha_nominal": krippendorff_alpha_nominal(valid),
        "alpha_threshold": KAPPA_THRESHOLD,
        "alpha_pass": (krippendorff_alpha_nominal(valid) or 0) >= KAPPA_THRESHOLD,
        "consensus_n": len(consensus),
        "disagreements_n": len(disagreements),
        "disagreements": [{"sample_id": row["sample_id"], "case_id": row["case_id"],
                           "a": row["a"], "b": row["b"],
                           "claim": row["claim_text"][:160]} for row in disagreements[:30]],
        "judge_comparison": {
            "matched_n": len(judge_rows),
            "unmatched_n": unmatched,
            "accuracy": judge_accuracy,
            "confusion": {f"{label}->judge_{'supported' if supported else 'unsupported'}": count
                          for (label, supported), count in sorted(confusion.items())},
            "judge_yes_rate_given_consensus_unsupported": judge_yes_rate_given_unsupported,
            "note": ("consensus 只含 A=B 且非 uncertain 的样本；judge 标签来自 "
                     "cross30_fresh_packet 的判定（meta 输出已剔除）；文本匹配失败的不计入。"),
        },
        "limitations": [
            "子 Agent 代审 ≠ 人工签名（human_checked=false）",
            "样本来自 cross30 单会议；n=100；α 只反映可判性/一致性，不代表标注正确率",
        ],
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# claim 级人工锚点校准（独立子 Agent 代审，2026-10-22）",
        "",
        "> 预注册：`evaluation/LEDGER.md §44`。两位独立子 Agent 盲标 100 条 claim，",
        "> 只给 question/claim/cited_context；**不是人工签名**（human_checked=false）。",
        "",
        "## 结果",
        "",
        "| 量 | 值 |",
        "|---|---|",
        f"| 有效配对 | {len(valid)} / {len(rows)} |",
        f"| 原始一致率 | {agreement:.3f} |" if agreement is not None else "| 原始一致率 | n/a |",
        f"| Cohen κ | {cohen_kappa(valid):.3f} |" if cohen_kappa(valid) is not None else "| Cohen κ | n/a |",
        f"| Krippendorff α（nominal） | {krippendorff_alpha_nominal(valid):.3f} |"
        if krippendorff_alpha_nominal(valid) is not None else "| Krippendorff α | n/a |",
        f"| α 目标（≥{KAPPA_THRESHOLD}） | {'PASS' if result['alpha_pass'] else 'FAIL'} |",
        f"| A 分布 | {result['label_distribution']['A']} |",
        f"| B 分布 | {result['label_distribution']['B']} |",
        f"| consensus（A=B 且非 uncertain） | {len(consensus)} |",
        "",
        "## 与 LLM judge 的对照（consensus 样本）",
        "",
        f"- 匹配到 judge claim：{len(judge_rows)} 条；未匹配：{unmatched} 条",
        f"- 一致率：{judge_accuracy:.3f}" if judge_accuracy is not None else "- 一致率：n/a",
        f"- 混淆：{result['judge_comparison']['confusion']}",
        "",
        "## 判定（按 §44）",
        "",
        f"- α {'≥' if result['alpha_pass'] else '<'} 0.70 → "
        f"{'claim 级裁判可作校准' if result['alpha_pass'] else '登记 claim 级裁判不可靠，faithfulness 继续只作方向信号'}",
        "- 子 Agent 代审不能冒充人工金标；正式金标仍需真人双标。",
        "",
    ]
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "n_pairs", "raw_agreement", "cohen_kappa", "krippendorff_alpha_nominal",
        "alpha_pass", "consensus_n", "disagreements_n")}, ensure_ascii=False, indent=2))
    print("judge:", result["judge_comparison"])
    print(f"→ {args.out_json}\n→ {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
