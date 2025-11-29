"""RAG 全链路质量闸：相对冻结基线回归 + 绝对下限 + 可检索事实召回（recall-v2），零模型调用。

阈值不是按当前分数“贴着”设的：基线冻结后只允许 2pp 波动；低于绝对下限或低于基线 2pp 才失败。
修改基线需要显式提交。

2026-10-22 仪器官改（recall-v2，见 evaluation/gold/fact_classes.json）：
- 旧 `context_recall` 把「检索材料按定义不可能包含」的 gold 事实算进分母（cross30 14 条缺失里
  初判 11 条是系统元数据/否定性结论；round2 独立验证用同题 packet 反证 3 条可检索后为 8 条），
  把可检索召回从 88.0% 压到 75.86%；
- 现在两条同时报：`context_recall`（旧口径，保持历史可比/冻结回归）与
  `context_recall_retrievable`（新口径，主指标；分母剔除 metadata/fixture/conflict；
  分类清单 sha256 入冻结基线，清单缺失/变更即 FAIL）；
  **换仪器 = 新读数不能与旧读数相减**，旧值继续作回归护栏。
- 新增：每个比例打印 Clopper-Pearson 95% 区间（小样本不报裸点估计）；
  校验冻结基线记录的 packet 校验和——产物变了而基线没重冻，闸门直接失败并提示重新冻结。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from lib import recall_quality, stats  # noqa: E402

RESULTS = EVAL / "results"
BASELINE = EVAL / "baselines/quality_gate_baseline.json"

METRICS = ("context_recall", "context_precision", "faithfulness", "answer_relevancy")
FLOORS = {
    "p0": {"context_recall": 0.90, "context_precision": 0.55,
           "faithfulness": 0.85, "answer_relevancy": 0.95},
    "cross30": {"context_recall": 0.70, "context_precision": 0.55,
                "faithfulness": 0.80, "answer_relevancy": 0.90},
    "new_meeting": {"context_recall": 0.80, "context_precision": 0.55,
                    "faithfulness": 0.75, "answer_relevancy": 0.90},
    "mixed100": {"context_recall": 0.85, "context_precision": 0.60,
                 "faithfulness": 0.80, "answer_relevancy": 0.90},
}
# recall-v2 的绝对下限：按"可检索口径"的当前水平留 2–4pp 余量，不贴当前分。
# recall-v2 绝对下限：与既有 floor 同一定位（防退化线；低于当前 3–5pp），不是改进目标；
# cross30 当前 88.0% / new_meeting 86.96%，0.90/0.85 的"改进目标"不入门禁。
RECALL_V2_FLOORS = {"p0": 0.95, "cross30": 0.85, "new_meeting": 0.82, "mixed100": 0.92}
SCOPE_TO_SET = {"p0": "cross_30", "cross30": "cross_30",
                "new_meeting": "new_meeting_15", "mixed100": "eval_100_mixed"}
P0_PATHS = [RESULTS / f"p0_fresh6_r{i}_packet.json" for i in (1, 2, 3)]
PACKET_PATHS = {
    "cross30": RESULTS / "cross30_fresh_packet.json",
    "new_meeting": RESULTS / "new_meeting_packet.json",
    "mixed100": RESULTS / "eval100_scope_fixed_packet.json",
}
CLAIM_PATH = RESULTS / "claim_audit_cross30_fresh.json"
TOLERANCE = 0.02
CLAIM_FLOOR = 0.15


def _rates(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    return {metric: float(summary[metric]["rate"]) for metric in METRICS
            if summary.get(metric) and summary.get(metric).get("rate") is not None}


def _summary(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("summary") or {}


def _canonical_manifest_sha(path: Path) -> str:
    """分类清单的**内容摘要**：剔除 generated_at 后按排序 key 序列化再 sha。

    标准重建命令（build_fact_classes.py）每次都会更新 generated_at；若直接对字节做 sha，
    重建一次就会让闸门误报"清单被改"。这里要检测的是**分类内容**变化，不是生成时间。
    """
    if not path.is_file():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("generated_at", None)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_artifacts(baseline: dict) -> list[str]:
    frozen = baseline.get("artifact_sha256") or {}
    if not frozen:
        return []
    problems = []
    for key, expected in frozen.items():
        path = RESULTS / key.split(":", 1)[1]
        if not path.is_file():
            problems.append(f"缺少产物 {key}")
            continue
        actual = _sha256(path)
        if actual != expected:
            problems.append(f"产物已变但基线未重冻: {key}")
    return problems


def _pooled(paths: list[Path], metric: str) -> dict:
    """多轮 packet 的合并计数（CI 用；显示的 value 仍是 mean-of-rates，二者口径在输出里注明）。"""
    successes = total = 0
    for path in paths:
        row = _summary(path).get(metric) or {}
        successes += int(row.get("successes") or 0)
        total += int(row.get("total") or 0)
    return {"successes": successes, "total": total}


def _ci(row: dict | None) -> str:
    if not row or not row.get("total"):
        return "n/a"
    interval = stats.clopper_pearson_interval(int(row["successes"]), int(row["total"]))
    return f"[{interval[0]:.3f},{interval[1]:.3f}]" if interval else "n/a"


def main() -> int:
    if not BASELINE.exists():
        print(f"缺少冻结基线: {BASELINE}")
        return 2
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    required = P0_PATHS + list(PACKET_PATHS.values()) + [CLAIM_PATH]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print(f"缺少评测产物（先跑对应评测/组包）: {missing}")
        return 2
    manifest = recall_quality.load_manifest()
    p0_packets = [json.loads(path.read_text(encoding="utf-8")) for path in P0_PATHS]
    packets = {scope: json.loads(path.read_text(encoding="utf-8"))
               for scope, path in PACKET_PATHS.items()}
    # 旧口径三行取均值（与冻结基线同法）；recall-v2 用逐题计数合并。
    p0 = {metric: round(sum(_rates(path)[metric] for path in P0_PATHS) / 3, 4)
          for metric in METRICS}
    values = {"p0": p0}
    for scope in packets:
        values[scope] = {metric: float(row["rate"]) for metric, row in
                         (_summary(PACKET_PATHS[scope]).items()) if metric in METRICS}
    claim = json.loads(CLAIM_PATH.read_text(encoding="utf-8"))
    claim_rate = float(claim.get("not_in_any_context_rate") or 0)

    rows = []
    failed = False
    artifact_problems = _check_artifacts(baseline)
    for scope, metrics in values.items():
        for metric, value in metrics.items():
            floor = FLOORS[scope][metric]
            frozen = float(baseline[scope][metric])
            ok = value >= floor and value >= frozen - TOLERANCE
            rows.append((scope, metric, value, frozen, value - frozen, floor, ok, "legacy"))
            failed = failed or not ok

    # recall-v2（主指标）：逐 scope 用清单剔除不可检索事实后重算
    v2_rows = []
    frozen_manifest_sha = baseline.get("fact_classes_sha256")
    manifest_sha = _canonical_manifest_sha(recall_quality.MANIFEST)
    manifest_problems = []
    if not frozen_manifest_sha:
        manifest_problems.append("基线缺少 fact_classes_sha256（主指标分类未纳管）")
    if not manifest_sha:
        manifest_problems.append("fact_classes.json 不存在（主指标 recall-v2 不可评）")
    elif frozen_manifest_sha and manifest_sha != frozen_manifest_sha:
        manifest_problems.append("fact_classes.json 内容已变但基线未重冻（主指标分类被改动）")
    failed = failed or bool(manifest_problems)
    for scope, set_name in SCOPE_TO_SET.items():
        if not recall_quality.has_set(set_name, manifest):
            manifest_problems.append(f"recall-v2 清单缺 {set_name}（{scope} 不可评）")
            failed = True
            continue
        packet_list = p0_packets if scope == "p0" else [packets[scope]]
        successes = total = excluded = 0
        for packet in packet_list:
            measured = recall_quality.retrievable_recall(packet, set_name, manifest)
            successes += measured["successes"]
            total += measured["total"]
            excluded += measured["excluded"]
        if not total:
            print(f"跳过 recall-v2 {scope}: 无可评事实")
            continue
        value = successes / total
        frozen = baseline.get("context_recall_retrievable", {}).get(scope)
        floor = RECALL_V2_FLOORS[scope]
        ok = value >= floor and (frozen is None or value >= float(frozen) - TOLERANCE)
        v2_rows.append((scope, value, successes, total, excluded, frozen, floor, ok))
        failed = failed or not ok

    frozen_claim = float(baseline["claim_not_in_any_context"])
    claim_ok = claim_rate <= CLAIM_FLOOR and claim_rate <= frozen_claim + TOLERANCE
    failed = failed or claim_ok is False
    if artifact_problems:
        failed = True

    print(f"{'scope':10} {'metric':24} {'value':>7} {'frozen':>7} {'delta':>7} {'floor':>7} ok  95%CI")
    smoke_packet = RESULTS / "new_meeting_scoped_packet.json"
    if smoke_packet.exists():
        for metric, row in (_summary(smoke_packet).items()):
            if metric in METRICS and isinstance(row, dict):
                print(f"{'smoke':10} {metric:24} {row['rate']:7.3f} {'-':>7} {'-':>7} "
                      f"{'-':>7} yes {_ci(row)}")
    p0_pooled = {metric: _pooled(P0_PATHS, metric) for metric in METRICS}
    for scope, metric, value, frozen, delta, floor, ok, _kind in rows:
        ci_row = p0_pooled.get(metric) if scope == "p0" else _summary(PACKET_PATHS[scope]).get(metric)
        print(f"{scope:10} {metric:24} {value:7.3f} {frozen:7.3f} {delta:+7.3f} {floor:7.3f} "
              f"{'yes' if ok else 'NO ':3} {_ci(ci_row)}")
    for scope, value, successes, total, excluded, frozen, floor, ok in v2_rows:
        delta = "-" if frozen is None else f"{value - float(frozen):+.3f}"
        frozen_text = "n/a" if frozen is None else f"{float(frozen):.3f}"
        interval = stats.clopper_pearson_interval(successes, total)
        ci = f"[{interval[0]:.3f},{interval[1]:.3f}]" if interval else "n/a"
        print(f"{scope:10} {'recall_retrievable':24} {value:7.3f} {frozen_text:>7} {delta:>7} "
              f"{floor:7.3f} {'yes' if ok else 'NO ':3} {ci} (n={successes}/{total}, "
              f"excluded={excluded})")
    claim_numbers = int(claim.get("numbers_checked") or 0)
    claim_bad = int(claim.get("unsupported_any_numbers") or 0)
    claim_ci = ("n/a" if not claim_numbers else
                (_ci({"successes": claim_bad, "total": claim_numbers}) + "（规则上界口径）"))
    print(f"{'cross30':10} {'claim_not_in_any_context':24} {claim_rate:7.3f} "
          f"{frozen_claim:7.3f} {claim_rate - frozen_claim:+7.3f} {CLAIM_FLOOR:7.3f} "
          f"{'yes' if claim_ok else 'NO ':3} {claim_ci}")
    print(f"baseline: {BASELINE} (tolerance {TOLERANCE:.2f}, frozen_at={baseline.get('frozen_at')})")
    print("注：p0 旧口径行的 CI 按三轮合并计数；显示值仍是 mean-of-rates（二者口径不同）")
    print(f"recall-v2 manifest: {recall_quality.MANIFEST.relative_to(EVAL.parent)} "
          f"(human_checked={bool(manifest.get('human_checked'))})")
    for problem in artifact_problems + manifest_problems:
        print(f"INSTRUMENT PROVENANCE FAIL: {problem}")
    if artifact_problems:
        print("提示：产物已更新——若变更是有意的，重新冻结基线（更新 artifact_sha256 与读数）。")
    print("QUALITY GATE:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
