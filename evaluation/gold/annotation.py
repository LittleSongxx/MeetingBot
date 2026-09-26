"""标注管线：**verification 协议**的标注包 + Krippendorff α（含逐类别）。

## 为什么是 verification 而不是独立标注

标注方法学的实测结论：**verification（先给出候选，让标注者判对/错/缺失）的
一致性显著高于独立标注**——因为后者测的是"标注者能否只看指南复现答案"。
本项目需要的两件事都天然适合 verification：

| 用途 | 候选来自 | 标注者要做的事 |
|---|---|---|
| 轴二 owner/deadline 金标 | 产品抽出的负责人/期限 | 判：对 / 错 / 该有但漏了 / 不适用 |
| 轴一 人机效度 | 模型判定给的档位（S/D/X/C） | 判：认同 / 不认同 / 判不了 |

## α 的口径（按发表惯例）

* 用 **Krippendorff's α**（支持任意标注者数、缺值、各类测量尺度），不用 Cohen's κ；
* **发表门槛 α ≥ 0.7**；**必须逐类别报**——整体 0.7 可能掩盖某一类 0.2；
* 标注者 **≥ 3 人**；聚合用人工裁决或多数投票，**并且必须同时公开原始标注**；
* `PABAK` 一并报：偏斜患病率会把 α 压扁（kappa paradox），只报 α 会误读成随机。

## 用法

    python evaluation/metrics/annotation.py --build-owner-deadline   # 零调用：生成待标包
    python evaluation/metrics/annotation.py --alpha labels.json      # 零调用：算 α（含逐类别）
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
DEFAULT_OWNER_PACKET = RESULTS_DIR / "annotation_owner_deadline.json"
DB_DIR = RESULTS_DIR / "db"

# verification 协议的标签：**互斥**，且刻意保留 `missing` 与 `na`——
# 少了它们，标注者会把"没提到"和"该有但漏了"挤进同一个桶，两类都没法解释。
OWNER_DEADLINE_LABELS = {
    "correct": "产品给的负责人/期限是对的（能在转写里找到）",
    "wrong": "产品给了，但转写里没有依据（编造）",
    "missing": "转写里明确说了，但产品没给（漏）",
    "na": "本条根本没有负责人/期限可言（不适用）",
    "unjudgeable": "判不了（乱码、指代不清）",
}

# 轴一的人机效度：把模型档位摆出来让标注者核对，而不是让它从零判
VALIDITY_LABELS = {
    "agree": "同意模型这个判定",
    "disagree": "不同意（我会判成别的档）",
    "unjudgeable": "判不了",
}


def _alpha_core(units: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """α 的核心计算（nominal），**含对角项**。

    重合矩阵 `n_ck` 要把 c == k 的对角项也累加进去：对角项不参与 Do，
    但**决定边际分布 n_c**，而边际决定 De。漏掉对角项会让 De 偏大、α 偏乐观
    （实测：手算 α = −0.1111 而漏掉对角项的实现在同一份数据上给 −0.5）。
    """
    coincidence: dict[tuple[Any, Any], float] = defaultdict(float)
    usable = 0
    total_codings = 0
    for unit in units:
        values = [value for value in unit if value is not None]
        if len(values) < 2:
            continue
        usable += 1
        total_codings += len(values)
        counts = Counter(values)
        m = len(values)
        for first, first_count in counts.items():
            for second, second_count in counts.items():
                # **不跳过 c == k**：对角项是边际的来源。
                # 对角项用 f(f−1)/(m−1)，**不是** f·f/(m−1)——后者会把"同一类"
                # 记成比实际更多的一致，α 随之偏向乐观（实测交错数据 α 被报成 0.0625，
                # 正确的 −0.75）。off-diagonal 才用 f·g/(m−1)。
                pair_count = (first_count * (first_count - 1) if first == second
                              else first_count * second_count)
                coincidence[(first, second)] += pair_count / (m - 1)
    if not usable:
        return {"alpha": None, "reason": "没有任何一个单位被 ≥2 位标注者标过"}
    n = sum(coincidence.values())
    if n <= 0:
        return {"alpha": None, "reason": "重合矩阵为空"}
    off_diagonal = sum(value for (first, second), value in coincidence.items()
                       if first != second)
    marginals: dict[Any, float] = defaultdict(float)
    for (first, _second), value in coincidence.items():
        marginals[first] += value
    expected = sum(first_marginal * second_marginal
                   for first, first_marginal in marginals.items()
                   for second, second_marginal in marginals.items()
                   if first != second
                   for second_marginal in [second_marginal]) / (n * (n - 1))
    if expected == 0:
        return {"alpha": 1.0, "units": usable, "pairs": n, "codings": total_codings,
                "reason": "所有标注都是同一类，De = 0，α 取 1"}
    observed = off_diagonal / n
    return {"alpha": 1 - observed / expected,
            "units": usable, "pairs": n, "codings": total_codings,
            "expected_disagreement": expected, "observed_disagreement": observed}


def krippendorff_alpha(units: Sequence[Sequence[Any]], *, level: str = "nominal"
                       ) -> dict[str, Any]:
    """Krippendorff's α（nominal）+ **逐类别 α**。`units` 是每个观测单位上各标注者的取值。

    公式：`α = 1 − Do/De`。缺值用 None，允许各单位标注者数不同。
    逐类别用"该类 vs 其余"的二分类 α，调用只做一次的核心计算。
    """
    if level != "nominal":
        raise ValueError("目前只实现了 nominal 尺度")
    core = _alpha_core(units)
    labels = sorted({value for unit in units for value in unit if value is not None}, key=str)
    per_category: dict[str, Any] = {}
    for label in labels:
        binary = [[("yes" if value == label else "no") if value is not None else None
                   for value in unit] for unit in units]
        per_category[str(label)] = {
            "alpha": _alpha_core(binary).get("alpha"),
            "codings": sum(1 for unit in units for value in unit if value == label),
        }
    return {**core, "per_category": per_category,
            "published_threshold": "α ≥ 0.7 才达到发表力度；逐类别必须一起报"}


def pabak(units: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """PABAK = 2p₀ − 1。α/κ 在偏斜患病率下会坍缩，PABAK 作为对照一起报。"""
    pairs = agree = 0
    for unit in units:
        values = [value for value in unit if value is not None]
        for index in range(len(values)):
            for other in range(index + 1, len(values)):
                pairs += 1
                agree += 1 if values[index] == values[other] else 0
    rate = agree / pairs if pairs else None
    return {"pairs": pairs, "agreement": rate, "pabak": (2 * rate - 1) if rate is not None else None}


_LIST_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")


def _slot_text(value):
    """槽位取值：v1 是裸字符串，v2 是 {text/raw/normalized, basis, evidence} 字典。"""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "raw", "value", "normalized", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return ""


def _minutes_payload(row) -> dict:
    """纪要行 → 六字段字典（库里是六个独立 JSON 列）。"""
    return {
        "summary": row.get("summary") or "",
        **{field: json.loads(row.get(f"{field}_json") or "[]") for field in _LIST_FIELDS},
    }


def build_owner_deadline_packet(*, sample: int = 120, seed: int = 20260926,
                                use_db: bool = True) -> dict[str, Any]:
    """从**真实库**与评测产物里抽 owner/deadline 候选，做成 verification 包。"""
    entries: list[dict[str, Any]] = []
    if use_db:
        minutes_path = DB_DIR / "minutes.json"
        if minutes_path.exists():
            for row in json.loads(minutes_path.read_text(encoding="utf-8")):
                payload = _minutes_payload(row)
                for field in ("decisions", "pending_items"):
                    for index, item in enumerate(payload.get(field) or []):
                        if not isinstance(item, Mapping):
                            continue
                        owner = _slot_text(item.get("owner")) or str(
                            item.get("owner_suggestion") or "")
                        deadline = _slot_text(item.get("deadline")) or str(
                            item.get("deadline_suggestion") or "")
                        content = str(item.get("content") or "")
                        if not content.strip():
                            continue
                        entries.append({
                            "source": "db", "minutes_id": row["id"], "field": field,
                            "index": index, "content": content,
                            "owner_candidate": owner, "deadline_candidate": deadline,
                        })
    rng = random.Random(seed)
    rng.shuffle(entries)
    picked = entries[:sample]
    items: list[dict[str, Any]] = []
    for position, entry in enumerate(picked, start=1):
        items.append({
            "anonymous_id": f"od_{position:03d}",
            "field": entry["field"],
            "content": entry["content"],
            # **verification 协议**：把候选摆出来，标注者只需判对/错/漏/不适用
            "owner_candidate": entry["owner_candidate"],
            "deadline_candidate": entry["deadline_candidate"],
            "labels": {"owner": None, "deadline": None},
            "note": "",
        })
    return {
        "packet_version": 1,
        "protocol": "verification（先给候选，再判对/错/漏/不适用）",
        "why_verification": ("verification 的一致性显著高于独立标注；"
                             "独立标注测的是标注者能否只看指南复现答案"),
        "labels_allowed": OWNER_DEADLINE_LABELS,
        "annotators_required": "≥3（Krippendorff α 支持任意人数，但发表惯例是 ≥3）",
        "publish_raw": "标注完成后**原始标注必须一起公开**，不能只公开聚合结果",
        "corpus_available": len(entries),
        "items": items,
    }


def build_validity_packet(packet_path: Path = RESULTS_DIR / "adjudication_packet_v2.json",
                          *, sample: int = 120, seed: int = 20260926) -> dict[str, Any]:
    """轴一的人机效度包：把**模型的档位**摆出来让标注者核对（verification）。"""
    if not packet_path.exists():
        raise SystemExit(f"缺少模型判定包：{packet_path}")
    source = json.loads(packet_path.read_text(encoding="utf-8"))
    rng = random.Random(seed)
    candidates: list[dict[str, Any]] = []
    for item in source.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        if not item.get("label"):
            continue  # 只取模型已判过的
        candidates.append({
            "item_text": item.get("item_text") or "",
            "transcript_window": item.get("transcript_window") or "",
            "model_label": item.get("label"),
        })
    rng.shuffle(candidates)
    items = [{"anonymous_id": f"val_{position:03d}", **entry, "human_label": None,
              "human_note": ""} for position, entry in enumerate(candidates[:sample], start=1)]
    return {
        "packet_version": 1,
        "purpose": "轴一 人机效度：人类是否认同模型的 S/D/X/C 判定",
        "protocol": "verification（模型档位已知，标注者判同意/不同意/判不了）",
        "labels_allowed": VALIDITY_LABELS,
        "corpus_with_model_labels": len(candidates),
        "items": items,
    }


def alpha_report(labels: Mapping[str, Any]) -> dict[str, Any]:
    """从 `{unit_id: {coder: label}}` 算 α + PABAK + 逐类别。"""
    units: list[list[Any]] = []
    coders: set[str] = set()
    for _unit_id, coding in labels.items():
        if not isinstance(coding, Mapping):
            continue
        coders.update(coding.keys())
        units.append([coding.get(coder) for coder in sorted(coding.keys())])
    return {
        "coders": sorted(coders),
        "units": len(units),
        "nominal": krippendorff_alpha(units),
        "pabak": pabak(units),
        "reading": (
            "只报 α 会被偏斜患病率误读成随机（kappa paradox）：必须同时看 PABAK 与逐类别 α。"
            "α ≥ 0.7 才达到发表力度。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-owner-deadline", action="store_true")
    parser.add_argument("--build-validity", action="store_true")
    parser.add_argument("--sample", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--alpha", type=Path, default=None,
                        help="已标注的 {unit_id: {coder: label}} JSON，算 α")
    parser.add_argument("--export-csv", type=Path, default=None,
                        help="把 owner/deadline 标注包导出成 CSV（每行一个待判项，众包平台直接可用）")
    parser.add_argument("--export-validity-csv", type=Path, default=None,
                        help="把 validity 四分类包导出成 CSV（裁判校准用：S/D/X/C 盲判）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.build_owner_deadline:
        packet = build_owner_deadline_packet(sample=args.sample, seed=args.seed)
        destination = args.output or DEFAULT_OWNER_PACKET
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {destination}：{len(packet['items'])} 条"
              f"（可抽语料 {packet['corpus_available']} 条）")
        return 0
    if args.build_validity:
        packet = build_validity_packet(sample=args.sample, seed=args.seed)
        destination = args.output or RESULTS_DIR / "annotation_validity.json"
        destination.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {destination}：{len(packet['items'])} 条"
              f"（模型已判语料 {packet['corpus_with_model_labels']} 条）")
        return 0
    if args.export_csv:
        import csv as _csv
        source = json.loads(DEFAULT_OWNER_PACKET.read_text(encoding="utf-8")) \
            if not args.output else json.loads(args.output.read_text(encoding="utf-8"))
        with args.export_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(["anonymous_id", "field", "content",
                             "owner_candidate", "deadline_candidate",
                             "owner_label", "deadline_label", "note"])
            for item in source["items"]:
                writer.writerow([item["anonymous_id"], item["field"], item["content"],
                                 item["owner_candidate"], item["deadline_candidate"],
                                 "", "", ""])
        print(f"wrote {args.export_csv}：{len(source['items'])} 行（标签列留空待填）")
        return 0
    if args.export_validity_csv:
        import csv as _csv
        source = json.loads((RESULTS_DIR / "annotation_validity.json").read_text(encoding="utf-8"))
        allowed = "/".join(source.get("labels_allowed") or [])
        with args.export_validity_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(["anonymous_id", "transcript_window", "item_text",
                             f"label ({allowed})", "confidence", "note"])
            for item in source["items"]:
                writer.writerow([item["anonymous_id"], item.get("transcript_window") or "",
                                 item.get("item_text") or "", "", "", ""])
        print(f"wrote {args.export_validity_csv}：{len(source['items'])} 行"
              f"（判据见 ANNOTATION_GUIDE.md；模型标签不在表内，盲判）")
        return 0
    if args.alpha:
        labels = json.loads(args.alpha.read_text(encoding="utf-8"))
        print(json.dumps(alpha_report(labels), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
