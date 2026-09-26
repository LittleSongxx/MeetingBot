"""语料钉定与装载（评测数据的唯一入口）。

从原 run_metrics 拆出：数据集路径钉住、VCSum 两代装载、契约 v2 双批合并断言。
引用任何一批数字前，先看这里的钉住常量——目录名是证据的一部分。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

EVALUATION_DIR = Path(__file__).resolve().parents[1]

PUBLIC_RESULTS = EVALUATION_DIR / "benchmarks" / "results"
VCSUM_RUNS = {
    "vcsum-test26": PUBLIC_RESULTS / "test26_combined" / "report.json",
    "vcsum-dev22": PUBLIC_RESULTS / "vcsum-dev22-20260925" / "report.json",
}

# 契约 v2（DeepSeek deepseek-flash + 6000 字分块 + 证据字段）下的 test26 全量运行。
#
# 为什么「钉住目录名」而不是自动发现最新一批：自动发现会让「这组数字是什么」随时间漂移
# ——再跑一次就在不知不觉中改写了对外引用过的历史结论。目录名是证据的一部分，因此
# 显式列出，并由合并函数断言它恰好覆盖 26 场、每场一个完成产物、模型一致。
#
# 合并依据（两批的用例集合不相交，合计恰好 26 场）：
#   * `2f6c5270`：22 场计划，21 场完成，`vcsum_21` 失败；
#   * `2edf738c`：只跑上面未完成的 5 场（`vcsum_21`/`108`/`172`/`201`/`208`），全部完成。
#     `vcsum_108` 是 test26 里最长的会议（38,804 字、812 轮），此前一路失败。
NEW_CONTRACT_TEST26_RUNS = (
    "vcsum-20260925T130415Z-2f6c5270",
    "vcsum-20260925T140012Z-2edf738c",
)
# 合并时逐场核对模型名，跨模型的产物不合并——那会把两个配置的数字混成一个。
NEW_CONTRACT_MODELS = ("deepseek-flash",)
NEW_CONTRACT_EXPECTED_CASES = 26
# The default dev manifest is a pinned 3-case subset; the stored dev report
# covers all 22 dev meetings, so the full manifest is the matching source.
VCSUM_ALL_DEV_MANIFEST = EVALUATION_DIR / "benchmarks" / "manifest_all_dev.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_vcsum_cases(split: str) -> list[dict[str, Any]]:
    """Rebuild VCSum transcripts from the pinned repository files, verified by hash."""
    sys.path.insert(0, str(EVALUATION_DIR / "benchmarks"))
    import vcsum_adapter  # type: ignore

    if split == "test26":
        manifest = vcsum_adapter.build_test_manifest()
        report_path = VCSUM_RUNS["vcsum-test26"]
    elif split == "dev22":
        manifest = vcsum_adapter.load_manifest(VCSUM_ALL_DEV_MANIFEST)
        report_path = VCSUM_RUNS["vcsum-dev22"]
    else:
        raise ValueError(f"unknown VCSum split: {split}")

    transcripts = {case["id"]: case for case in manifest["cases"]}
    report = json.loads(report_path.read_text(encoding="utf-8"))

    cases: list[dict[str, Any]] = []
    for record in report.get("records", []):
        case_id = record.get("case_id")
        entry = transcripts.get(case_id)
        if not entry:
            continue
        if vcsum_adapter.sha256_text(entry["transcript"]) != entry["transcript_sha256"]:
            raise ValueError(f"VCSum transcript checksum mismatch for {case_id}")
        baseline = (record.get("baseline") or {}).get("output")
        revised = (record.get("revised") or {}).get("output")
        if not isinstance(baseline, Mapping):
            continue
        cases.append({
            "case_id": case_id,
            "transcript": entry["transcript"],
            "reference_overall": entry.get("reference_overall"),
            "gold": None,
            "baseline_output": baseline,
            "revised_output": revised if isinstance(revised, Mapping) else baseline,
            "record": record,
        })
    return cases


def _record_models(record: Mapping[str, Any]) -> set[str]:
    return {
        str(log.get("model_name") or "")
        for log in (record.get("ai_call_logs") or [])
        if isinstance(log, Mapping) and log.get("model_name")
    }


# 单遍长上下文（2026-10-06 产品改造后）的 test26 运行钉定。
# 目录名在首轮单遍运行完成后填入（与 v2 同一套"目录名是证据"纪律）；
# 在此之前引用本集合会得到明确的报错而不是空结果。
SINGLE_PASS_TEST26_RUNS: tuple[str, ...] = ()


def load_single_pass_cases(
    run_names: Sequence[str] = SINGLE_PASS_TEST26_RUNS,
    *,
    results_root: Path | None = None,
) -> list[dict[str, Any]]:
    """单遍长上下文的 test26 集：沿用 v2 的合并与断言规则（每场一个完成产物）。"""
    if not run_names:
        raise ValueError(
            "单遍运行的目录尚未登记：首轮单遍 test26 跑完后，"
            "把 results/ 下的运行目录名填入 corpora.SINGLE_PASS_TEST26_RUNS"
        )
    return load_new_contract_cases(run_names, expected_models=("qwen3.8-max-0902",),
                                   expected_cases=26, results_root=results_root)


def load_new_contract_cases(
    run_names: Sequence[str] = NEW_CONTRACT_TEST26_RUNS,
    *,
    expected_models: Sequence[str] = NEW_CONTRACT_MODELS,
    expected_cases: int = NEW_CONTRACT_EXPECTED_CASES,
    results_root: Path | None = None,
) -> list[dict[str, Any]]:
    """契约 v2 的 test26 全量集：把两批运行按 case_id 合并成一场一个完成产物。

    这个函数存在的理由，是那批**对外引用的数字**此前只能由一次性脚本算出：目录名、
    合并规则、locatability 的归一化方式都只留在会话里。任何人不重新踩一遍就复现不了，
    这是「结论可审计」的直接缺口。所以规则写进代码，并在这里**断言**而不是默默接受：
    用例数、模型名、完成状态、每场只有一个产物，任一不满足就报错而不是出一组看似正常的数字。
    """
    root = results_root or PUBLIC_RESULTS
    sys.path.insert(0, str(EVALUATION_DIR / "benchmarks"))
    import vcsum_adapter  # type: ignore

    manifest = vcsum_adapter.build_test_manifest()
    entries = {case["id"]: case for case in manifest["cases"]}

    merged: dict[str, dict[str, Any]] = {}
    provenance: dict[str, str] = {}
    for name in run_names:
        report_path = root / name / "report.json"
        if not report_path.exists():
            raise FileNotFoundError(f"契约 v2 运行目录缺失：{report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for record in report.get("records", []):
            if str(record.get("status")) != "completed":
                continue
            case_id = str(record.get("case_id") or "")
            if case_id in merged:
                # 同一场出现在两批里 = 合并规则的前提不成立，宁可报错也不要挑一个
                raise ValueError(
                    f"case {case_id} 在两个运行目录里都有完成产物（{provenance[case_id]}、{name}），"
                    "合并规则要求两批不相交；请显式指定要采用哪一批。"
                )
            models = _record_models(record)
            if models and set(expected_models) != models:
                raise ValueError(
                    f"case {case_id} 的模型为 {sorted(models)}，与声明的 {list(expected_models)} 不符；"
                    "跨模型的产物不合并。"
                )
            entry = entries.get(case_id)
            if entry is None:
                raise ValueError(f"case {case_id} 不在 test26 清单里（{name}）")
            if vcsum_adapter.sha256_text(entry["transcript"]) != entry["transcript_sha256"]:
                raise ValueError(f"VCSum transcript checksum mismatch for {case_id}")
            baseline = (record.get("baseline") or {}).get("output")
            if not isinstance(baseline, Mapping):
                raise ValueError(f"case {case_id} 缺少初稿输出（{name}）")
            revised = (record.get("revised") or {}).get("output")
            merged[case_id] = {
                "case_id": case_id,
                "transcript": entry["transcript"],
                "reference_overall": entry.get("reference_overall"),
                "gold": None,
                "baseline_output": baseline,
                "revised_output": revised if isinstance(revised, Mapping) else baseline,
                "record": record,
            }
            provenance[case_id] = name

    if len(merged) != expected_cases:
        raise ValueError(
            f"合并后得到 {len(merged)} 场，与声明的 {expected_cases} 场不符；"
            f"缺少：{sorted(set(entries) - set(merged))}"
        )
    cases = [merged[case_id] for case_id in sorted(merged)]
    for case in cases:
        case["run_dir"] = provenance[case["case_id"]]
    return cases


# --------------------------------------------------------------------------- #
# Aggregate report
# --------------------------------------------------------------------------- #

