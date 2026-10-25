"""Claim 级人工锚点包（零模型调用）：为 context-faithfulness 校准准备待标 CSV。

背景：RAG 指标的 faithfulness/attribution 全部来自 LLM 裁判；评测宪法要求"单裁判读数
只是方向信号"。人工锚点包把「claim 是否被其**引用的**上下文支撑」做成可双人独立标注的
表格，用于估 judge 的 TPR/TNR 与一致率（Krippendorff α 可复用 gold/annotation.py 的
alpha 口径）。

标注口径（写进 CSV 的表头，众包/团队都能直接做）：
- supported：claim 的实质内容能在它引用的引用快照（cited_context）里找到（允许同义改写/正确归纳）；
- unsupported：找不到，或与上下文相反；
- uncertain：判不了（引文被截断、语境不足）。
**只判"该 claim 相对它自己引用的 refs"**，不引入外部知识。

用法：
    python3 evaluation/gold/claim_anchor.py --packet evaluation/results/cross30_fresh_packet.json \
        --output evaluation/gold/claim_anchor_cross30_100.csv --limit 100
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
CITATION = re.compile(r"\[(\d{1,3})\]")


def _claims(answer: str) -> list[dict]:
    """有引用的断言（按句读/换行切；太短的标题行跳过）。"""
    rows: list[dict] = []
    for raw in re.split(r"(?<=[。！？])|\n", answer or ""):
        text = raw.strip()
        if len(text) < 8:
            continue
        refs = sorted({int(match.group(1)) for match in CITATION.finditer(text)})
        refs = [ref for ref in refs if ref >= 1]  # 引用编号从 1 开始；[0] 不是合法引用
        if refs:
            rows.append({"text": text, "refs": refs})
    return rows


def build(packet_path: Path, limit: int) -> list[dict]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for case in packet.get("cases") or []:
        by_ref = {int(item.get("ref")): item for item in case.get("contexts") or []
                  if item.get("ref") is not None}
        claims = _claims(str(case.get("answer") or ""))
        for claim in claims:
            cited = []
            for ref in claim["refs"]:
                item = by_ref.get(ref) or {}
                text = (item.get("text") or item.get("source_text") or item.get("quote") or "")
                cited.append(f"[{ref}] {str(text)[:800]}")
            rows.append({
                "packet": packet_path.name,
                "case_id": case.get("case_id"),
                "question": case.get("question"),
                "claim_text": claim["text"][:1200],
                "cited_refs": ",".join(str(ref) for ref in claim["refs"]),
                "cited_context": "\n".join(cited)[:2400],
            })
            if len(rows) >= limit * 3:  # 先多收集，再按题分层抽取
                break
        if len(rows) >= limit * 3:
            break
    # 简单分层：按 case 轮转取，避免所有样本来自前几题
    by_case: dict[str, list[dict]] = {}
    for row in rows:
        by_case.setdefault(str(row["case_id"]), []).append(row)
    picked: list[dict] = []
    case_ids = sorted(by_case)
    cursor = 0
    while len(picked) < limit and any(by_case.values()):
        case_id = case_ids[cursor % len(case_ids)]
        bucket = by_case[case_id]
        if bucket:
            picked.append(bucket.pop(0))
        cursor += 1
    for index, row in enumerate(picked, start=1):
        row["sample_id"] = f"ca{index:03d}"
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    rows = build(args.packet, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "sample_id", "packet", "case_id", "question", "claim_text", "cited_refs",
            "cited_context",
            "annotator_A_verdict", "annotator_A_note",
            "annotator_B_verdict", "annotator_B_note",
            "adjudicated_verdict",
        ])
        for row in rows:
            writer.writerow([
                row["sample_id"], row["packet"], row["case_id"], row["question"],
                row["claim_text"], row["cited_refs"], row["cited_context"],
                "", "", "", "", "",
            ])
    print(f"wrote {args.output}: {len(rows)} claims（verdict 列留空待双人标注）")
    print("verdict 取值：supported / unsupported / uncertain（只判 cited_context 是否支撑该 claim）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
