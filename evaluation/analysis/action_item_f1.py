"""S3：产品 pending_items vs 10 场 AI-draft 行动项 gold 的抽取 F1（零模型调用）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from common import textsim  # noqa: E402

GOLD = EVAL / "gold/vcsum_action_items_ai.json"
RUN = EVAL / "benchmarks/results/vcsum-test26-preservation-20261005"
OUT = EVAL / "results/action_item_gold_eval.json"
MD = EVAL / "reports/2026-10-23_action_item_f1.md"


def norm(text: object) -> str:
    return textsim.normalize(text)


def product_items(case_id: str) -> list[dict]:
    path = RUN / f"{case_id}.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    output = ((payload.get("revised") or {}).get("output") or {})
    return [item for item in (output.get("pending_items") or []) if isinstance(item, dict)]


def content_of(item: dict) -> str:
    return str(item.get("content") or "")


def slot_text(item: dict, key: str) -> str:
    value = item.get(key)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("normalized") or "")
    if key == "owner":
        return str(item.get("owner_name") or item.get("owner_suggestion") or "")
    if key == "deadline":
        return str(item.get("deadline_text") or item.get("deadline_suggestion") or "")
    return str(value or "")


def match_items(gold_items: list[dict], product: list[dict]) -> list[tuple[int, int, float]]:
    candidates = []
    for gi, gold in enumerate(gold_items):
        for pi, item in enumerate(product):
            first, second = norm(content_of(gold)), norm(content_of(item))
            if textsim.is_same_item(first, second):
                ratio, _ = textsim.similarity(first, second)
                candidates.append((ratio, gi, pi))
    candidates.sort(reverse=True)
    used_gold, used_product, pairs = set(), set(), []
    for ratio, gi, pi in candidates:
        if gi in used_gold or pi in used_product:
            continue
        used_gold.add(gi)
        used_product.add(pi)
        pairs.append((gi, pi, ratio))
    return pairs


def main() -> int:
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    rows = []
    total_gold = total_product = total_matched = 0
    owner_checked = owner_agree = 0
    deadline_checked = deadline_agree = 0
    for case in gold["cases"]:
        case_id = case["case_id"]
        gold_items = case.get("items") or []
        product = product_items(case_id)
        pairs = match_items(gold_items, product)
        total_gold += len(gold_items)
        total_product += len(product)
        total_matched += len(pairs)
        for gi, pi, _ in pairs:
            gold_owner = norm(slot_text(gold_items[gi], "owner"))
            product_owner = norm(slot_text(product[pi], "owner"))
            if gold_owner:
                owner_checked += 1
                if product_owner and (product_owner in gold_owner or gold_owner in product_owner):
                    owner_agree += 1
            gold_deadline = norm(slot_text(gold_items[gi], "deadline"))
            product_deadline = norm(slot_text(product[pi], "deadline"))
            if gold_deadline:
                deadline_checked += 1
                if product_deadline and (product_deadline in gold_deadline
                                        or gold_deadline in product_deadline):
                    deadline_agree += 1
        rows.append({"case_id": case_id, "gold": len(gold_items), "product": len(product),
                     "matched": len(pairs),
                     "gold_contents": [content_of(item) for item in gold_items],
                     "product_contents": [content_of(item) for item in product]})
    precision = total_matched / total_product if total_product else None
    recall = total_matched / total_gold if total_gold else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall > 0 else None)
    result = {
        "policy": "LEDGER §54：10 场 AI-draft gold（human_checked=false），内容匹配 F1",
        "gold": str(GOLD.relative_to(EVAL)),
        "product_run": str(RUN.relative_to(EVAL)),
        "items": {"gold": total_gold, "product": total_product, "matched": total_matched},
        "precision": precision, "recall": recall, "f1": f1,
        "owner_agreement": {"checked": owner_checked, "agree": owner_agree,
                            "rate": (owner_agree / owner_checked) if owner_checked else None},
        "deadline_agreement": {"checked": deadline_checked, "agree": deadline_agree,
                               "rate": (deadline_agree / deadline_checked) if deadline_checked else None},
        "target_f1": 0.70,
        "pass": bool(f1 is not None and f1 >= 0.70),
        "by_case": rows,
        "limitations": [
            "gold 为 AI-draft（deepseek，human_checked=false），只作方向信号",
            "内容匹配用 textsim.is_same_item（产品/评测共用的条目身份判据），可能漏近义改写",
            "产品 pending_items 来自 26 场 preservation 运行；10 场是其中固定子集",
        ],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 行动项抽取 F1：产品 pending_items vs 10 场 AI-draft gold（2026-10-23）",
        "",
        "> 预注册 `evaluation/LEDGER.md §54`；gold 为 deepseek AI-draft（human_checked=false），",
        "> 只作方向信号；内容是 rule/proxy 匹配，不是人工语义判定。",
        "",
        "## 结果",
        "",
        "| 量 | 值 |",
        "|---|---|",
        f"| gold items | {total_gold} |",
        f"| product items | {total_product} |",
        f"| matched | {total_matched} |",
        f"| precision | {precision:.3f} |" if precision is not None else "| precision | n/a |",
        f"| recall | {recall:.3f} |" if recall is not None else "| recall | n/a |",
        f"| **F1（目标 ≥0.70）** | **{f1:.3f}** |" if f1 is not None else "| F1 | n/a |",
        f"| 判定 | {'PASS' if result['pass'] else 'FAIL'} |",
        f"| owner 一致（有 owner 的匹配对） | {owner_agree}/{owner_checked} |",
        f"| deadline 一致 | {deadline_agree}/{deadline_checked} |",
        "",
        "## 逐场",
        "",
        "| case | gold | product | matched |",
        "|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| {row['case_id']} | {row['gold']} | {row['product']} | {row['matched']} |")
    lines += ["", "## 限制", ""] + [f"- {item}" for item in result["limitations"]]
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("items", "precision", "recall", "f1", "pass",
                       "owner_agreement", "deadline_agreement")}, ensure_ascii=False, indent=2))
    print(f"→ {OUT}\n→ {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
