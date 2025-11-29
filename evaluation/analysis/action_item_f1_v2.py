"""S3 仪器 v2b：两族一致（deepseek gold ∩ qwen 抽取）作为共识 gold，再评产品。零调用。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from common import textsim  # noqa: E402

GOLD = EVAL / "gold/vcsum_action_items_ai_v2.json"
RUN = EVAL / "benchmarks/results/vcsum-test26-preservation-20261005"
OUT = EVAL / "results/action_item_f1_v2.json"
MD = EVAL / "reports/2026-10-23_action_item_f1_v2.md"


def norm(text) -> str:
    return textsim.normalize(str(text or ""))


def match(left: list[dict], right: list[dict]) -> list[tuple[int, int]]:
    candidates = []
    for li, litem in enumerate(left):
        for ri, ritem in enumerate(right):
            a, b = norm(litem.get("content")), norm(ritem.get("content"))
            if textsim.is_same_item(a, b):
                ratio, _ = textsim.similarity(a, b)
                candidates.append((ratio, li, ri))
    candidates.sort(reverse=True)
    used_l, used_r, pairs = set(), set(), []
    for _, li, ri in candidates:
        if li in used_l or ri in used_r:
            continue
        used_l.add(li)
        used_r.add(ri)
        pairs.append((li, ri))
    return pairs


def product_items(case_id: str) -> list[dict]:
    path = RUN / f"{case_id}.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in (((payload.get("revised") or {}).get("output") or {})
                              .get("pending_items") or []) if isinstance(item, dict)]


def main() -> int:
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    total_consensus = total_product = matched = 0
    rows = []
    for case in gold["cases"]:
        case_id = case["case_id"]
        deepseek_items = case.get("gold") or []
        qwen_items = case.get("qwen") or []
        consensus = [deepseek_items[li] for li, _ in match(deepseek_items, qwen_items)]
        product = product_items(case_id)
        pairs = match(consensus, product)
        total_consensus += len(consensus)
        total_product += len(product)
        matched += len(pairs)
        rows.append({"case_id": case_id, "gold_deepseek": len(deepseek_items),
                     "qwen": len(qwen_items), "consensus": len(consensus),
                     "product": len(product), "matched": len(pairs)})
    precision = matched / total_product if total_product else None
    recall = matched / total_consensus if total_consensus else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and precision + recall > 0 else None)
    result = {
        "instrument": "two-family consensus gold（deepseek ∩ qwen，严格定义 v2）",
        "items": {"consensus_gold": total_consensus, "product": total_product,
                  "matched": matched},
        "precision": precision, "recall": recall, "f1": f1,
        "target_f1": 0.70, "pass": bool(f1 is not None and f1 >= 0.70),
        "by_case": rows,
        "limitations": [
            "严格定义下 gold 极稀疏（10 场中多数 0 条），共识 gold n 很小，F1 不稳定",
            "两族一致只降低 rubric 噪声，不替代人工金标",
            "VCSum 是论坛/访谈型会议，多数没有真正的行动项；抽取 F1 的样本效率低",
        ],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 行动项抽取仪器 v2：两族共识 gold 与产品 F1（2026-10-23）",
        "",
        "> 预注册 §55；严格定义下 gold 极稀疏，本报告是**仪器可行性**结果，不是稳定质量读数。",
        "",
        "| 量 | 值 |",
        "|---|---|",
        f"| 共识 gold（deepseek ∩ qwen） | {total_consensus} |",
        f"| 产品 pending_items | {total_product} |",
        f"| matched | {matched} |",
        f"| precision | {precision if precision is None else round(precision, 3)} |",
        f"| recall | {recall if recall is None else round(recall, 3)} |",
        f"| F1 | {f1 if f1 is None else round(f1, 3)} |",
        "",
        "## 逐场",
        "",
        "| case | deepseek | qwen | consensus | product | matched |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| {row['case_id']} | {row['gold_deepseek']} | {row['qwen']} | "
                     f"{row['consensus']} | {row['product']} | {row['matched']} |")
    lines += ["", "## 限制", ""] + [f"- {item}" for item in result["limitations"]]
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("items", "precision", "recall", "f1", "pass")},
                     ensure_ascii=False, indent=2))
    print(f"→ {OUT}\n→ {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
