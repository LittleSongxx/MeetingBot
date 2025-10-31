"""Holdout 双臂的**内容级**判定：词法代理饱和后唯一能分辨优化轮效果的仪器。

## 预注册（看数据之前写死）

- 抽样：每场每臂固定种子抽 5 个列表条目（topics/viewpoints/decisions/
  pending_items/risks），同一场的两臂分别独立抽取（条目不同——比的是分布不是配对条目）；
- 判定：qwen 盲态四分类（与 ai_adjudicate.SYSTEM_PROMPT 逐字相同，含 D1–D5），
  窗口 = 条目在产品格式转写中的定位段；
- 主读数：条目级 grounded 率（supported+derivable 占比），两臂各 n≈30；
  逐场配对（n=6）Wilcoxon（wilcox+pratt 并报）；
- 次读数：partial/unsupported 分布（无依据内容的形态）。

边界：单裁判（qwen）+ n=6 场——与既有全部内容级读数同样的限制；
结论口径是"方向把关"，不是效果宣称。

运行：PYTHONPATH=evaluation python evaluation/metrics/holdout_content_judgment.py \
          --authorized-llm-calls 65
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import item_judge as ai_adjudicate  # type: ignore
    from judges.packets import _transcript_window  # type: ignore
    from lib import model_client  # type: ignore
else:
    from judges import item_judge as ai_adjudicate
    from judges.packets import _transcript_window
    from lib import model_client

RESULTS = Path(__file__).resolve().parents[1] / "results"
_PB = Path(__file__).resolve().parents[1] / "benchmarks"
HOLDOUT_DIR = _PB / "results" / "holdout-validation-20260926"
MANIFEST = _PB / "manifest_holdout6.json"
FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")
SEED = 20260926
PER_CASE = 5


def _product_format(case: dict[str, Any]) -> str:
    return "\n".join(
        f"[{int(s.get('start_ms', 0)) // 1000}s][{s.get('speaker', '')}] {s.get('text', '')}"
        for s in (case.get("segments") or []))


def sample_items() -> dict[tuple[str, str], list[dict[str, Any]]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    samples: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for arm in ("baseline", "treated"):
        rng = random.Random(SEED + (0 if arm == "baseline" else 1))
        for case in manifest["cases"]:
            path = HOLDOUT_DIR / f"{case['id']}.{arm}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            output = payload.get("baseline_output")
            if not isinstance(output, dict):
                continue
            pool = []
            for field in FIELDS:
                for item in (output.get(field) or []):
                    text = str(item.get("content") or item.get("title") or "")
                    if text:
                        pool.append({"field": field, "item_text": text})
            rng.shuffle(pool)
            for entry in pool[:PER_CASE]:
                entry["transcript_window"] = _transcript_window(
                    _product_format(case), entry["item_text"])
                samples[(case["id"], arm)] = samples.get((case["id"], arm), []) + [entry]
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized-llm-calls", type=int, required=True)
    parser.add_argument("--set", default="holdout6", choices=("holdout6", "extra8"))
    args = parser.parse_args()

    global HOLDOUT_DIR, MANIFEST
    if args.set == "extra8":
        HOLDOUT_DIR = _PB / "results" / "holdout-extra8-20260926"
        MANIFEST = _PB / "manifest_holdout_extra8.json"
    out_path = RESULTS / f"holdout_content_judgment_{args.set}.json"
    state: dict[str, Any] = {"purpose": __doc__, "labels": {}}
    if out_path.exists():
        state = json.loads(out_path.read_text(encoding="utf-8"))
    samples = sample_items()
    state["n_sampled"] = {f"{k[0]}.{k[1]}": len(v) for k, v in samples.items()}
    url, model, key = model_client.endpoint("qwen")
    calls = 0
    for (case_id, arm), entries in sorted(samples.items()):
        for position, entry in enumerate(entries, 1):
            anon = f"hc_{case_id}_{arm}_{position:02d}"
            if state["labels"].get(anon, {}).get("label"):
                continue
            if calls >= args.authorized_llm_calls:
                print(f"停止：授权用尽（{calls}）", file=sys.stderr)
                break
            user = (f"【转写窗口】\n{entry['transcript_window']}\n\n"
                    f"【纪要条目】\n{entry['item_text']}\n\n请给出你的标签。")
            try:
                reply = model_client.call_json(url, model, key,
                                               ai_adjudicate.SYSTEM_PROMPT, user,
                                               total_deadline=180)
                label, operation, reason = ai_adjudicate._parse_label(reply["parsed"])
                state["labels"][anon] = {"label": label, "operation": operation,
                                         "reason": reason, "arm": arm, "case_id": case_id}
            except (model_client.ModelClientError, ValueError) as error:
                state["labels"][anon] = {"error": str(error)[:200], "arm": arm,
                                         "case_id": case_id}
            calls += 1
            out_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            time.sleep(0.15)

    # ---- 汇总（预注册主读数）----
    from lib import stats
    per_case: dict[tuple[str, str], list[str]] = {}
    for record in state["labels"].values():
        if record.get("label"):
            per_case.setdefault((record["case_id"], record["arm"]), []).append(record["label"])
    def grounded_rate(labels: list[str]) -> float:
        return sum(1 for x in labels if x in ("supported", "derivable")) / len(labels)
    summary: dict[str, Any] = {"per_arm": {}, "per_case": {}}
    diffs = []
    for arm in ("baseline", "treated"):
        rates = [grounded_rate(v) for k, v in per_case.items() if k[1] == arm]
        labels_all = [x for k, v in per_case.items() if k[1] == arm for x in v]
        from collections import Counter
        summary["per_arm"][arm] = {
            "n_items": len(labels_all),
            "mean_grounded_rate": round(sum(rates) / len(rates), 4) if rates else None,
            "label_distribution": dict(Counter(labels_all)),
        }
    for case_id in sorted({k[0] for k in per_case}):
        b = per_case.get((case_id, "baseline"), [])
        t = per_case.get((case_id, "treated"), [])
        summary["per_case"][case_id] = {
            "baseline": round(grounded_rate(b), 3) if b else None,
            "treated": round(grounded_rate(t), 3) if t else None,
        }
        if b and t:
            diffs.append(grounded_rate(t) - grounded_rate(b))
    summary["paired_wilcoxon"] = stats.wilcoxon_signed_rank(diffs) if diffs else None
    state["summary"] = summary
    out_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["per_arm"], ensure_ascii=False, indent=1))
    print("per_case:", json.dumps(summary["per_case"], ensure_ascii=False))
    print(f"calls={calls} → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
