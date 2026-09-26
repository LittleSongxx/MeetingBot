"""三臂对照分析：隔离「多调一次」与「审查意见」的效应。

| 臂 | 含义 | 数据来源 |
|---|---|---|
| `baseline` | 一次生成的初稿 | 契约 v2 那批运行的 `baseline.output` |
| `passthrough` | 初稿 → 重写（意见清单为空） | `run_passthrough_arm.py` 的产物 |
| `reflected` | 初稿 → 审查 → 重写 | 同一批运行的 `revised.output` |

三个两两对比的含义不同，**结论必须挂在具体的那一对上**：

* `baseline → passthrough`：纯粹多调一次模型并重新编码的效应；
* `baseline → reflected`：上述效应 **加上** 审查意见的效应；
* **`passthrough → reflected`：审查意见的净效应**——这一对才是产品主张的检验。

## 主结局在看数据之前就定下来（预注册）

本项目在指标选择上栽过一次（同一批数据既校准又报收益，报出"静默删除 −82%"后不得不撤回），
因此这里**先写死主结局**再看数：

* **主结局 1（措辞）**：无依据原子率（词法代理，逐会议配对）。
* **主结局 2（删除）**：与初稿相比"措辞无处留存"的条目数（逐会议配对）。
  注意：本批数据来自**不含保默认机制**的构建，因此删除类指标在臂间是真实差异；
  重建镜像后这条主结局会被保默认掩盖，**那时必须换掉它**。

次要结局：条目数、正文字符数、内容支撑率（仅 decisions/pending_items，见 README 的限定）。

## 统计口径

逐会议配对（Wilcoxon 符号秩，剔除零差并报告数量），n=26；零差处理方式影响 p 值，
因此 `wilcox` 与 `pratt` 两个约定都报。所有比例给 Clopper–Pearson 精确区间。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from regression import grounding as g  # type: ignore
    from lib import corpora as rm, stats  # type: ignore
else:
    from regression import grounding as g
    from lib import corpora as rm, stats

ARMS = ("baseline", "passthrough", "reflected")
DEFAULT_ARM_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "passthrough-arm-20260925"
CONTENT_SUPPORT_FIELDS = ("decisions", "pending_items")


def _item_texts(field: str, output: Mapping[str, Any]) -> list[str]:
    if field == "summary":
        summary = output.get("summary")
        return [summary] if isinstance(summary, str) and summary.strip() else []
    items = output.get(field) or []
    texts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        text = g.item_text_for_content(field, item)
        if text:
            texts.append(text)
    return texts


def arm_metrics(output: Mapping[str, Any], transcript: str, baseline: Mapping[str, Any]) -> dict[str, Any]:
    """一份输出在一条臂上的全部指标。"""
    index = g.build_transcript_index(transcript)
    checkable = 0
    unsupported = 0
    per_field: dict[str, dict[str, int]] = {}
    content_support: list[float] = []
    item_count = 0
    char_count = 0
    disappeared = 0

    for field in ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks"):
        texts = _item_texts(field, output)
        item_count += len(texts)
        char_count += sum(len(text) for text in texts)
        field_checkable = 0
        field_unsupported = 0
        for item in ([output.get(field)] if field == "summary" else (output.get(field) or [])):
            if item is None:
                continue
            analysis = g.analyze_item(field, item, index)
            field_checkable += analysis["checkable_count"]
            field_unsupported += len(analysis["unsupported_atoms"])
            if field in CONTENT_SUPPORT_FIELDS:
                content_support.append(float(analysis["content_support"]["ratio"]))
        checkable += field_checkable
        unsupported += field_unsupported
        per_field[field] = {"checkable": field_checkable, "unsupported": field_unsupported}

    # 与初稿相比"措辞无处留存"的条目数：这是删除类主结局
    for field in ("topics", "viewpoints", "decisions", "pending_items", "risks"):
        baseline_texts = [g.normalize(t) for t in _item_texts(field, baseline)]
        output_texts = [g.normalize(t) for t in _item_texts(field, output)]
        reversed_pool = output_texts
        for text in baseline_texts:
            if text and not g.shared_matches_any(text, reversed_pool):
                disappeared += 1

    return {
        "item_count": item_count,
        "char_count": char_count,
        "checkable_atoms": checkable,
        "unsupported_atoms": unsupported,
        "unsupported_atom_rate": stats.proportion_report(unsupported, checkable) if checkable else None,
        "content_support_mean": (sum(content_support) / len(content_support)) if content_support else None,
        "disappeared_items": disappeared,
        "per_field": per_field,
    }


def is_degenerate(output: Mapping[str, Any], baseline: Mapping[str, Any],
                  *, min_baseline_items: int = 5) -> bool:
    """这份输出是不是"结构合法但内容空了"。

    实测踩到（2026-09-25）：`vcsum_21` 的修订稿只有一段 627 字的 summary，
    五个列表字段**全为空**，而初稿有 63 条。产物里 `structure.schema_valid=true`、
    `diagnostic_model_passed=true`、状态 `WAITING_CONFIRMATION`——也就是说它**通过了结构校验
    并被判定为"自检通过"**，等人点确认就把 63 条全删掉。

    成因在 `raw_model_output` 里看得很清楚：模型只返回了 `{summary}`，
    而抢救层把它补成"六字段齐全 + 五个空数组"，于是结构合法、内容归零。

    这类产物必须在分析里**显式剔除**，否则"消失条目"会被它独占：
    实测 80 条消失里 63 条来自这一场，扣掉之后是 17 条 / 25 场。
    """
    fields = ("topics", "viewpoints", "decisions", "pending_items", "risks")
    baseline_items = sum(len(baseline.get(f) or []) for f in fields)
    output_items = sum(len(output.get(f) or []) for f in fields)
    return baseline_items >= min_baseline_items and output_items == 0


def _paired_deletions(arm_series: Sequence[float]) -> dict[str, Any]:
    """删除类主结局的配对：**每个臂与"隐式的零"配对**，不与初稿的 0 配对。

    初稿的 `disappeared_items` 按定义是 None（与自身相比），不能拿它当第一列：
    那样 `zip` 会得到空序列，于是报告成"0 个非零对"——看起来像"没有删除"，
    实际是**根本没比**。这就是本项目反复出现的"无声的零"，我自己也踩了一次。
    """
    zeros = [0.0] * len(arm_series)
    result = _paired(zeros, list(arm_series))
    result["baseline_reference"] = "与初稿相比消失的条目数（初稿自身恒为 0）"
    result["n_meetings"] = len(arm_series)
    return result


def _paired(first: Sequence[float], second: Sequence[float]) -> dict[str, Any]:
    """逐会议配对比较：第二个减第一个（负数 = 更少 = 通常是改善方向）。"""
    differences = [b - a for a, b in zip(first, second)]
    nonzero = [d for d in differences if d != 0]
    improved = sum(1 for d in differences if d < 0)
    worsened = sum(1 for d in differences if d > 0)
    return {
        "n_meetings": len(differences),
        "n_nonzero": len(nonzero),
        "mean_difference": (sum(differences) / len(differences)) if differences else None,
        "improved": improved,
        "worsened": worsened,
        "unchanged": len(differences) - improved - worsened,
        "wilcoxon": stats.wilcoxon_signed_rank(differences),
        "sign_test": stats.mcnemar_exact(worsened, improved),
        "bootstrap_mean_difference": stats.paired_bootstrap_mean_ci(differences),
    }


def build_report(cases: Sequence[Mapping[str, Any]], arm_outputs: Mapping[str, Mapping[str, Any]],
                 arm_dir: Path) -> dict[str, Any]:
    per_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    missing: list[str] = []
    for case in cases:
        case_id = case["case_id"]
        transcript = case["transcript"]
        baseline = case["baseline_output"]
        outputs = {
            "baseline": baseline,
            "reflected": case.get("revised_output"),
            "passthrough": arm_outputs.get(case_id),
        }
        if not isinstance(outputs["passthrough"], Mapping):
            missing.append(case_id)
        for arm, output in outputs.items():
            if not isinstance(output, Mapping):
                continue
            metrics = arm_metrics(output, transcript, baseline)
            metrics["degenerate"] = (arm != "baseline"
                                     and is_degenerate(output, baseline))
            if arm == "baseline":
                # 与自身相比必然为 0；报 0 会被读成"没有删除"，实际是「不适用」
                metrics["disappeared_items"] = None
            if metrics["degenerate"]:
                # 退化产物的"消失条目"不是审查删的，是整篇内容归零；单列而不混入
                metrics["disappeared_items_excluded_reason"] = "degenerate_output"
                metrics["disappeared_items"] = None
            per_arm[arm].append({"case_id": case_id, **metrics})

    # 每一对比较**只在自己有效的场次上配对**，不共用一个全局"可比集合"：
    # 实测教训——早先所有比较都被 passthrough 的有无门控，于是缺对照的场次会把
    # baseline→reflected 也一起清空，报告成"0 个非零对"（看起来像"没有差异"）。
    def _by_case(arm: str) -> dict[str, dict[str, Any]]:
        return {r["case_id"]: r for r in per_arm[arm]}

    def _series(arm: str, key: str) -> list[float]:
        return [float(r[key]) for r in per_arm[arm] if r.get(key) is not None]

    def _aligned(arm_a: str, arm_b: str, key: str) -> tuple[list[float], list[float]]:
        other = _by_case(arm_b)
        first: list[float] = []
        second: list[float] = []
        for record in per_arm[arm_a]:
            peer = other.get(record["case_id"])
            if peer is None:
                continue
            left, right = record.get(key), peer.get(key)
            if left is None or right is None:
                continue
            first.append(float(left))
            second.append(float(right))
        return first, second

    def _rate_series(arm: str) -> list[float]:
        values: list[float] = []
        for record in per_arm[arm]:
            rate = (record["unsupported_atom_rate"] or {}).get("rate")
            if rate is not None:
                values.append(float(rate))
        return values

    def _aligned_rates(arm_a: str, arm_b: str) -> tuple[list[float], list[float]]:
        other = _by_case(arm_b)
        first: list[float] = []
        second: list[float] = []
        for record in per_arm[arm_a]:
            peer = other.get(record["case_id"])
            if peer is None:
                continue
            left = (record["unsupported_atom_rate"] or {}).get("rate")
            right = (peer["unsupported_atom_rate"] or {}).get("rate")
            if left is None or right is None:
                continue
            first.append(float(left))
            second.append(float(right))
        return first, second

    comparisons = {}
    for name, arm_a, arm_b in (
        ("baseline_to_passthrough", "baseline", "passthrough"),
        ("baseline_to_reflected", "baseline", "reflected"),
        ("passthrough_to_reflected", "passthrough", "reflected"),
    ):
        a_rates, b_rates = _aligned_rates(arm_a, arm_b)
        # 删除类主结局的语义随对比对象而变，**不能一律用同一种配对**：
        #   * baseline→X：初稿自身恒为 0，所以 X 的条数就是它与初稿的差（隐含零参照）；
        #   * X→Y（两臂都不是初稿）：必须用两臂的**配对差**——若也拿 Y 与零参照比，
        #     就会把"两臂都在删"报成"Y 比 X 删得多"（实测踩过：passthrough→reflected
        #     一度报出 p=0.002，而真实的臂间差是 −0.04、p=0.22）。
        if arm_a == "baseline":
            deletions = _paired_deletions(_series(arm_b, "disappeared_items"))
        else:
            deletions = _paired(*_aligned(arm_a, arm_b, "disappeared_items"))
            deletions["note"] = "两臂配对差（不是各自与零参照比）"
        comparisons[name] = {
            "unsupported_atom_rate": _paired(a_rates, b_rates),
            "disappeared_items": deletions,
            "item_count": _paired(*_aligned(arm_a, arm_b, "item_count")),
            "char_count": _paired(*_aligned(arm_a, arm_b, "char_count")),
        }

    totals: dict[str, Any] = {}
    for arm, records in per_arm.items():
        checkable = sum(r["checkable_atoms"] for r in records)
        unsupported = sum(r["unsupported_atoms"] for r in records)
        disappeared = [r["disappeared_items"] for r in records if r["disappeared_items"] is not None]
        totals[arm] = {
            "meetings": len(records),
            "items": sum(r["item_count"] for r in records),
            "chars": sum(r["char_count"] for r in records),
            "checkable_atoms": checkable,
            "unsupported_atoms": unsupported,
            "unsupported_atom_rate": stats.proportion_report(unsupported, checkable) if checkable else None,
            "disappeared_items": sum(disappeared) if disappeared else None,
            "disappeared_per_meeting": (sum(disappeared) / len(disappeared)) if disappeared else None,
        }

    return {
        "arm_directory": str(arm_dir),
        "meetings_with_passthrough": len(per_arm["passthrough"]),
        "meetings_missing_passthrough": missing,
        "primary_endpoints": ["unsupported_atom_rate", "disappeared_items"],
        "degenerate_outputs": {
            arm: [r["case_id"] for r in records if r.get("degenerate")]
            for arm, records in per_arm.items() if any(r.get("degenerate") for r in records)
        },
        "primary_note": (
            "主结局在看数据之前写死（见模块 docstring）。`passthrough_to_reflected` 才是"
            "审查意见的净效应；另外两对都含「多调一次」的效应。"
        ),
        "arm_totals": totals,
        "comparisons": comparisons,
        "per_meeting": {arm: records for arm, records in per_arm.items()},
        "notes": [
            "无依据原子率是**词法代理**：只覆盖日期、数量、具名实体，看不见改写与蕴含。",
            "内容支撑率只在 decisions/pending_items 上有区分力，因此只在这两个字段上聚合。",
            "本批数据来自不含保默认机制的构建，删除类指标因此可用；重建镜像后需换主结局。",
        ],
    }


def format_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# 三臂对照：空反馈重写 vs 真实自检",
        "",
        f"有对照产物的会议 {report['meetings_with_passthrough']} 场"
        f"（缺对照的：{report['meetings_missing_passthrough'] or '无'}）。",
        "",
        "| 臂 | 会议 | 条目 | 正文字符 | 可核查原子 | 无依据原子 | 无依据率 | 与初稿相比消失的条目 |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for arm, totals in report["arm_totals"].items():
        rate = totals["unsupported_atom_rate"] or {}
        disappeared = (totals["disappeared_items"] if totals["disappeared_items"] is None
                       else f"{totals['disappeared_items']}（{totals['disappeared_per_meeting']:.2f}/场）")
        lines.append(
            f"| {arm} | {totals['meetings']} | {totals['items']} | {totals['chars']} | "
            f"{totals['checkable_atoms']} | {totals['unsupported_atoms']} | "
            f"{100 * (rate.get('rate') or 0):.2f}% | "
            f"{'不适用（与自身相比）' if disappeared is None else disappeared} |"
        )
    lines += ["", "## 逐会议配对（主结局）", "",
              "| 对比 | 结局 | 非零对 | 改善 | 恶化 | Wilcoxon p | 符号检验 p |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for name, block in report["comparisons"].items():
        for endpoint, paired in block.items():
            wilcoxon_p = (paired["wilcoxon"] or {}).get("p_value_two_sided")
            sign_p = (paired["sign_test"] or {}).get("p_value_two_sided")
            lines.append(
                f"| {name} | {endpoint} | {paired['n_nonzero']} | {paired['improved']} | "
                f"{paired['worsened']} | {'n/a' if wilcoxon_p is None else f'{wilcoxon_p:.4f}'} | "
                f"{'n/a' if sign_p is None else f'{sign_p:.4f}'} |"
            )
    lines += ["", report["primary_note"], ""]
    for note in report["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def load_arm_outputs(arm_dir: Path) -> dict[str, Mapping[str, Any]]:
    outputs: dict[str, Mapping[str, Any]] = {}
    for path in sorted(arm_dir.glob("*.json")):
        if path.name == "dispatch-ledger.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(payload.get("output"), Mapping):
            outputs[str(payload.get("case_id") or path.stem)] = payload["output"]
    return outputs


def _window(transcript: str, item_text: str, padding: int = 400) -> str:
    """取条目在转写里的来源窗口。复用裁决模块的同一实现，不另写一份。"""
    if __package__ in (None, ""):
        import adjudication  # type: ignore
        return adjudication._transcript_window(transcript, item_text, padding=padding)
    from judges import packets as adjudication  # type: ignore
    return adjudication._transcript_window(transcript, item_text, padding=padding)


def build_deletion_packet(
    cases: Sequence[Mapping[str, Any]],
    arm_outputs: Mapping[str, Mapping[str, Any]],
    *,
    deleted_sample: int = 20,
    kept_sample: int = 20,
    seed: int = 20260925,
) -> dict[str, Any]:
    """删除质量裁决包：**被删掉的条目是「无依据（该删）」还是「有依据（过度修订）」？**

    为什么必须靠裁决：实测 80 条消失条目里 **75 条（94%）没有任何可核查原子**——
    日期、数量、具名实体三类词法代理对它们完全无话可说。也就是说「删得对不对」
    这个问题在现有离线指标里**根本无法回答**，只能靠内容级判定。

    对照设计：同时抽**保留下来的**条目。若被删条目与保留下来的在「有依据」比例上
    没有差别，说明删除是**不分青红皂白**的；若被删的明显更低，说明审查在按依据筛选。
    """
    rng = random.Random(seed)
    deleted: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    fields = ("topics", "viewpoints", "decisions", "pending_items", "risks")

    for case in cases:
        case_id = case["case_id"]
        transcript = case["transcript"]
        baseline = case["baseline_output"]
        for arm, output in (("reflected", case.get("revised_output")),
                            ("passthrough", arm_outputs.get(case_id))):
            if not isinstance(output, Mapping):
                continue
            for field in fields:
                out_texts = [g.normalize(g.item_text_for_content(field, x))
                             for x in (output.get(field) or [])]
                for item in baseline.get(field) or []:
                    text = g.item_text_for_content(field, item)
                    normalized = g.normalize(text)
                    if not normalized:
                        continue
                    entry = {
                        "case_id": case_id,
                        "field": field,
                        "item_text": text,
                        "transcript_window": _window(transcript, text),
                        "arm": arm,
                    }
                    if g.shared_matches_any(normalized, out_texts):
                        kept.append(entry)
                    else:
                        deleted.append(entry)

    rng.shuffle(deleted)
    rng.shuffle(kept)
    sampled = ([{**e, "stratum": f"deleted_{e['arm']}"} for e in deleted[:deleted_sample]]
               + [{**e, "stratum": "kept"} for e in kept[:kept_sample]])
    rng.shuffle(sampled)

    if __package__ in (None, ""):
        import adjudication  # type: ignore
    else:
        from judges import packets as adjudication  # type: ignore
    items: list[dict[str, Any]] = []
    key: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(sampled, start=1):
        anonymous_id = f"del_{position:03d}"
        items.append({
            "anonymous_id": anonymous_id,
            "field": entry["field"],
            "item_text": entry["item_text"],
            "transcript_window": entry["transcript_window"],
            "label": None,
            "note": "",
        })
        key[anonymous_id] = {"stratum": entry["stratum"], "case_id": entry["case_id"],
                             "version": "baseline", "reviewer_issue_type": None,
                             "proxy_flagged_atom": None}
    return {
        "packet_version": 3,
        "purpose": "删除质量：被删条目 vs 保留下来的条目，谁更有依据",
        "blinding": (
            "items are shuffled and anonymously numbered; whether an item was deleted or kept "
            "is withheld until labels are recorded"
        ),
        "label_guide": adjudication.LABEL_GUIDE,
        "labels_allowed": list(adjudication.LABELS),
        "corpus_counts": {"deleted_total": len(deleted), "kept_total": len(kept)},
        "available_by_stratum": {
            "deleted_passthrough": sum(1 for e in deleted if e["arm"] == "passthrough"),
            "deleted_reflected": sum(1 for e in deleted if e["arm"] == "reflected"),
            "kept": len(kept),
        },
        "items": items,
        "_key": key,
    }


def summarize_deletion_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """按层汇总裁决标签：被删条目里有多少是「有依据」的（= 过度修订）。"""
    key = packet["_key"]
    per_stratum: dict[str, dict[str, int]] = {}
    for item in packet["items"]:
        if not item.get("label"):
            continue
        stratum = key[item["anonymous_id"]]["stratum"]
        bucket = per_stratum.setdefault(stratum, {})
        bucket[item["label"]] = bucket.get(item["label"], 0) + 1
    rates: dict[str, Any] = {}
    for stratum, counts in sorted(per_stratum.items()):
        total = sum(counts.values())
        grounded = counts.get("supported", 0) + counts.get("derivable", 0)
        rates[stratum] = {
            "labelled": total,
            "labels": counts,
            "grounded_count": grounded,
            "grounded_rate": stats.proportion_report(grounded, total) if total else None,
        }
    return {
        "by_stratum": rates,
        "reading": (
            "`grounded_rate` 是被裁决者判为有依据的比例。被删条目若高，说明删掉了有依据的内容"
            "（过度修订）；若明显低于保留下来的条目，说明审查在按依据筛选，删除是有区分的。"
        ),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm-dir", type=Path, default=DEFAULT_ARM_DIR)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "results" / "arm_comparison.json")
    parser.add_argument("--build-deletion-packet", action="store_true",
                        help="另外生成删除质量裁决包（供 ai_adjudicate 标注）")
    parser.add_argument("--deleted-sample", type=int, default=20)
    parser.add_argument("--kept-sample", type=int, default=20)
    args = parser.parse_args(list(argv) if argv is not None else None)

    cases = rm.load_new_contract_cases()
    outputs = load_arm_outputs(args.arm_dir)

    if args.build_deletion_packet:
        packet = build_deletion_packet(cases, outputs, deleted_sample=args.deleted_sample,
                                       kept_sample=args.kept_sample)
        packet_path = args.output.with_name("deletion_packet.json")
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {packet_path} with {len(packet['items'])} items")
        print(json.dumps({"corpus_counts": packet["corpus_counts"],
                          "available_by_stratum": packet["available_by_stratum"]}, ensure_ascii=False))
        return 0
    report = build_report(cases, outputs, args.arm_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(format_markdown(report), encoding="utf-8")
    print(f"wrote {args.output}")
    print(f"wrote {markdown_path}")
    print(json.dumps({arm: {k: v for k, v in totals.items() if k != "per_field"}
                      for arm, totals in report["arm_totals"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
