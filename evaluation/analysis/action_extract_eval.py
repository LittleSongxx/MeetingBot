"""S3 可行性：独立抽取步（qwen）vs AI-draft gold（deepseek）的抽取 F1（10 场）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))

from common import textsim  # noqa: E402
from lib import model_client  # noqa: E402

MANIFEST = EVAL / "benchmarks/manifest_test_chunk6000.json"
GOLD = EVAL / "gold/vcsum_action_items_ai.json"
OUT = EVAL / "results/action_extract_qwen.json"
MD = EVAL / "reports/2026-10-23_action_extract_qwen.md"

SYSTEM = """你是会议行动项抽取器。输入一场会议的完整转写。
只抽取明确的行动项/承诺/后续安排（谁要做什么、何时完成、交付什么），不抽泛泛讨论。
每条输出：content（一句话，动宾结构）、owner（原文明确负责人，否则 null）、
deadline（原文明确期限，否则 null）、evidence_quote（原文逐字≤120字）。
只输出 JSON：{"items":[...]}；没有则 {"items":[]}。"""


def norm(text: object) -> str:
    return textsim.normalize(str(text or ""))


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in manifest["cases"]}
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    url, model, key = model_client.endpoint("qwen")
    rows = []
    total_gold = total_pred = matched = 0
    for case in gold["cases"]:
        case_id = case["case_id"]
        transcript = cases[case_id]["transcript"]
        try:
            parsed = model_client.call_json(
                url, model, key, SYSTEM, f"会议 {case_id} 转写：\n\n{transcript}",
                timeout=240, total_deadline=360, max_tokens=4000, thinking=False)
        except model_client.ModelClientError as error:
            rows.append({"case_id": case_id, "error": str(error)[:200]})
            continue
        payload = parsed.get("parsed") or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        items = items if isinstance(items, list) else []
        gold_items = case.get("items") or []
        total_gold += len(gold_items)
        total_pred += len(items)
        pairs = []
        for gi, gold_item in enumerate(gold_items):
            for pi, item in enumerate(items):
                if textsim.is_same_item(norm(gold_item.get("content")), norm(item.get("content"))):
                    ratio, _ = textsim.similarity(norm(gold_item.get("content")),
                                                 norm(item.get("content")))
                    pairs.append((ratio, gi, pi))
        pairs.sort(reverse=True)
        used_g, used_p = set(), set()
        for _, gi, pi in pairs:
            if gi in used_g or pi in used_p:
                continue
            used_g.add(gi)
            used_p.add(pi)
        matched += len(used_g)
        rows.append({"case_id": case_id, "gold": len(gold_items), "pred": len(items),
                     "matched": len(used_g)})
        print(case_id, "gold", len(gold_items), "pred", len(items), "matched", len(used_g))
    precision = matched / total_pred if total_pred else None
    recall = matched / total_gold if total_gold else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and precision + recall > 0 else None)
    result = {"method": "qwen 独立抽取 vs deepseek AI-draft gold（跨族、同一 rubric）",
              "items": {"gold": total_gold, "pred": total_pred, "matched": matched},
              "precision": precision, "recall": recall, "f1": f1,
              "target_f1": 0.70, "pass": bool(f1 is not None and f1 >= 0.70),
              "cases": rows,
              "limitation": "gold 是 AI-draft（human_checked=false）；跨族只能作方向信号"}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 独立行动项抽取步（qwen）vs AI-draft gold（deepseek）：10 场",
        "",
        "> 预注册 §54 的后续：比较“独立抽取步”与产品纪要 pending_items 相对 gold 的召回/精度。",
        "> gold 为 AI-draft（human_checked=false），跨族只作方向信号。",
        "",
        "| 量 | 值 |",
        "|---|---|",
        f"| gold items | {total_gold} |",
        f"| 抽取 items | {total_pred} |",
        f"| matched | {matched} |",
        f"| precision | {precision:.3f} |" if precision is not None else "| precision | n/a |",
        f"| recall | {recall:.3f} |" if recall is not None else "| recall | n/a |",
        f"| **F1（目标 ≥0.70）** | **{f1:.3f}** |" if f1 is not None else "| F1 | n/a |",
        f"| 判定 | {'PASS' if result['pass'] else 'FAIL'} |",
        "",
        "## 与现有产品 pending_items 对比",
        "",
        "| 臂 | gold | pred | matched | P | R | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| 产品 pending_items（同 10 场） | 29 | 17 | 4 | 0.235 | 0.138 | 0.174 |",
        f"| 独立抽取步（qwen） | {total_gold} | {total_pred} | {matched} | "
        f"{precision:.3f} | {recall:.3f} | {f1:.3f} |",
        "",
    ]
    MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("items", "precision", "recall", "f1", "pass")},
                     ensure_ascii=False, indent=2))
    print(f"→ {OUT}\n→ {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
