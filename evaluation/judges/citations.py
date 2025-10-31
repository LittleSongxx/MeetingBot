"""引用级评测（ALCE 式）：证据**是否支撑**断言，而不只是"能不能定位"。

## 为什么这是最该补的一项

本项目此前的"证据可定位率 97.64%"只回答"引用能不能在转写里逐字找到"，
**不回答"这段引用是否支撑这条断言"**。业界（ALCE, EMNLP 2023）的 citation precision
要求的是后者，而且它是可自动化的、不需要人工金标的那一类。

契约 v2 已经**强制每条条目带 evidence**（实测 4,022 条 / 26 场 ≈ 155 条/场，
是词法原子级（10.3/场）的 **17 倍**），因此这项测量现在零成本可得——不需要产品加字段。

## 两个口径都要报（ALCE 的教训）

ALCE 用 leave-one-out 判"这条引用是不是多余的"，代价高且只对"引用整篇文档"成立。
这里按**断言级**做，并且两个口径都报：

* **citation precision**：被引证据里有多少**支撑**该条目 → 直接对应 ALCE 的 precision；
* **item support rate**：有多少条目**至少有一条支撑性引用** → 对应 ALCE 的 recall 侧；
* 同时报 **unnecessary citation 比例**（引用多条但只有一条支撑）——ALCE 称之为
  shotgun citation，是"堆引用凑数"的直接度量。

## 与"无依据率"的区别

无依据率是**词法代理**（看不到改写与蕴含）；本模块是**判定性**的（模型判蕴含），
因此它才是能与业界数字对比的那一类。但**它仍是模型判定，不是人类金标**：
产出里带 `adjudicator_disclosure`。

## 用法

    # 1) 生成样本（零模型调用）
    python evaluation/metrics/citation_metrics.py --build --sample 60
    # 2) 逐条判定（每次调用判一条目的全部引用）
    python evaluation/metrics/citation_metrics.py --judge --authorized-llm-calls 60 --provider qwen
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import packets as A  # type: ignore
    from regression import grounding as g  # type: ignore
    from lib import corpora as rm, model_client, stats  # type: ignore
else:
    from judges import packets as A
    from regression import grounding as g
    from lib import corpora as rm, model_client, stats

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
DEFAULT_PACKET = RESULTS_DIR / "citation_packet.json"
FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")

JUDGE_SYSTEM = """你在检查**会议纪要的引用质量**。

我会给你：一条纪要内容、它的转写来源窗口，以及这条内容所引用的若干段原文引用。
你的任务：逐条判断**这段引用是否支撑这条内容**。

判定规则：
1. 引用能在窗口里逐字定位 → 这是前提，不是通过条件；还要看它是否真的**支持**该内容。
2. 支撑 = 引用所陈述的事实足以让读者接受这条内容（允许改写、压缩、正确推导）。
3. 不支撑 = 引用与该内容无关、只沾到边缘、或该内容的关键具体信息（数字、日期、人名、范围）
   在引用里找不到。
4. 只依据给你的窗口，不要用你自己的领域知识补全。

只输出 JSON：
{"citations": [{"index": 1, "supports": true, "why": "一句话"}, ...],
 "core_supported": true 或 false,
 "unnecessary": [1, 2]}"""


def build_packet(sample: int = 60, seed: int = 20260926) -> dict[str, Any]:
    """抽条目样本，连同它们的引用。**只用契约 v2 产物，不用旧契约**（旧产物没有证据）。"""
    cases = rm.load_new_contract_cases()
    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    for case in cases:
        transcript = case["transcript"]
        output = case["baseline_output"]
        if not isinstance(output, Mapping):
            continue
        for field in FIELDS:
            for index, item in enumerate(output.get(field) or []):
                if not isinstance(item, Mapping):
                    continue
                evidence = [e for e in (item.get("evidence") or []) if isinstance(e, Mapping)]
                text = g.item_text_for_content(field, item)
                if not evidence or not text or len(g.normalize(text)) < 12:
                    continue
                items.append({
                    "case_id": case["case_id"],
                    "field": field,
                    "index": index,
                    "item_text": text,
                    "citations": [str(e.get("quote") or "") for e in evidence],
                    "window": A._transcript_window(transcript, text),
                })
    rng.shuffle(items)
    sample_items = items[:sample]
    for position, item in enumerate(sample_items, start=1):
        item["anonymous_id"] = f"cit_{position:03d}"
    return {
        "packet_version": 1,
        "purpose": "引用级精度：证据是否**支撑**断言（ALCE 式），而非仅能否定位",
        "source": "vcsum-test26-v2 的初稿条目",
        "corpus_items_with_citations": len(items),
        "items": sample_items,
    }


def judge_packet(packet: dict[str, Any], *, output_path: Path, authorized_calls: int,
                 provider: str = "qwen",
                 progress=print) -> dict[str, Any]:
    url, model, key = model_client.endpoint(provider)
    pending = [i for i in packet["items"] if not i.get("judgement")]
    if authorized_calls < len(pending):
        progress(f"授权 {authorized_calls} < 待判 {len(pending)}，本次只判前 {authorized_calls} 条")
        pending = pending[:authorized_calls]
    packet.setdefault("adjudicator_disclosure", {
        "model_used": model, "provider": provider,
        "definition": "支撑判定（允许改写/压缩/正确推导）——与业界 citation precision 同口径",
        "not_gold": "这是模型判定，不是人类金标",
    })
    calls = 0
    for position, item in enumerate(pending, start=1):
        listing = "\n".join(f"{n}. {q}" for n, q in enumerate(item["citations"], start=1))
        user = (f"【纪要内容】（字段：{item['field']}）\n{item['item_text']}\n\n"
                f"【转写窗口】\n{item['window']}\n\n"
                f"【这条内容引用的原文】\n{listing}\n\n请逐条判断并输出 JSON。")
        try:
            reply = model_client.call_json(url, model, key, JUDGE_SYSTEM, user,
                                           total_deadline=240)
            item["judgement"] = reply["parsed"]
        except model_client.ModelClientError as error:
            item["judgement_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        calls += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(f"  [{position}/{len(pending)}] {item['anonymous_id']} "
                 f"→ {'失败' if item.get('judgement_error') else '已判'}")
        time.sleep(0.2)
    return {"provider": provider, "model": model, "calls_made": calls,
            "judged": sum(1 for i in packet["items"] if i.get("judgement"))}


def summarize(packet: Mapping[str, Any]) -> dict[str, Any]:
    """citation precision（引用级）与 item support rate（条目级）都要报。"""
    total_cites = supported_cites = 0
    items_supported = items_judged = 0
    unnecessary = 0
    per_field: dict[str, dict[str, int]] = {}
    for item in packet["items"]:
        judgement = item.get("judgement")
        if not isinstance(judgement, Mapping):
            continue
        items_judged += 1
        citations = judgement.get("citations")
        flags: list[bool] = []
        if isinstance(citations, list):
            for entry in citations:
                if isinstance(entry, Mapping) and "supports" in entry:
                    flags.append(bool(entry["supports"]))
        total_cites += len(flags)
        supported_cites += sum(1 for f in flags if f)
        core = judgement.get("core_supported")
        if bool(core):
            items_supported += 1
        unnecessary += len(judgement.get("unnecessary") or [])
        bucket = per_field.setdefault(item["field"], {"cites": 0, "supported": 0, "items": 0, "items_ok": 0})
        bucket["cites"] += len(flags)
        bucket["supported"] += sum(1 for f in flags if f)
        bucket["items"] += 1
        bucket["items_ok"] += 1 if bool(core) else 0
    return {
        "items_judged": items_judged,
        "citation_precision": stats.proportion_report(supported_cites, total_cites) if total_cites else None,
        "item_support_rate": stats.proportion_report(items_supported, items_judged) if items_judged else None,
        "unnecessary_citation_count": unnecessary,
        "unnecessary_citation_rate": (unnecessary / total_cites) if total_cites else None,
        "per_field": per_field,
        "reading": (
            "citation_precision = 被引证据中**支撑**该条目的比例（对应 ALCE 的 precision）；"
            "item_support_rate = 至少有一条支撑性引用的条目比例（对应 recall 侧）。"
            "两者都按**宽松档**判定（允许改写/压缩/正确推导），因此可与业界的"
            "事实一致性/引用质量数字对照。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--provider", default="qwen", choices=sorted(model_client.PROVIDERS))
    parser.add_argument("--authorized-llm-calls", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.build:
        packet = build_packet(sample=args.sample)
        args.packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.packet}: 样本 {len(packet['items'])} 条"
              f"（语料里带引用的条目 {packet['corpus_items_with_citations']} 条）")
        return 0
    if args.judge:
        if not args.authorized_llm_calls:
            print("拒绝执行：--judge 必须显式传 --authorized-llm-calls", file=sys.stderr)
            return 2
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        summary = judge_packet(packet, output_path=args.packet,
                               authorized_calls=args.authorized_llm_calls, provider=args.provider)
        packet["summary"] = summarize(packet)
        args.packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        print(json.dumps(packet["summary"], ensure_ascii=False, indent=2)[:1500])
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
