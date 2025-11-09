"""覆盖损失的阶段归因诊断：漏记发生在**分段抽取**还是**合并**？

## 为什么建这个

完整性 v3 给出"纪要漏了哪些点"，但不回答损失发生在哪一段。Map-Reduce 链路
有两个候选损失点：分段抽取（该段转写里的点根本没被抽成条目）与合并
（分段里明明有、合并后消失）。不修对段落，优化就是盲调。

## 方法

对每场会议，从运行产物的 `{case}.responses.json` 取各 `MINUTES_CHUNK` 的
`parsed_output`（分段部分纪要，逐调用留存的证据），合成"分段并集"伪纪要：
五个列表字段直接拼接、summary 按段序连排——这正是合并步实际看到的输入的并集。
然后用**与完整性 v3 相同的清单、相同的裁判**判并集覆盖：

* 抽取损失 = 1 − 并集覆盖率（点在各分段里都没被抽出来）；
* 合并损失 = 并集覆盖率 − 合并后（baseline）覆盖率（抽出来了但合并丢了）。

单块会议并集恒等于 baseline（无合并步），作为对照臂，delta 应接近 0。

## 纪律

* 这是**诊断**，不是产品指标：不设阈值、不进 LEDGER 结论表、不对外引用；
  结论只回答"修哪段"。
* 机械 4-gram 覆盖只作交叉参照，并明文标注：该口径曾因"机制与端点共用
  同一机械"被作废（LEDGER 撤回表），此处仅用于方向核对。
* 判定走与 v3 相同的 `judge_coverage`（否定式、thinking=ON），逐场落盘可续跑。

## 运行方式

需要模型凭据，必须显式传 `--authorized-llm-calls`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):  # allow direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from judges import completeness  # type: ignore
    from lib import corpora, model_client  # type: ignore
else:
    from judges import completeness
    from lib import corpora, model_client

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
LIST_FIELDS = ("topics", "viewpoints", "decisions", "pending_items", "risks")


def find_responses_path(case_id: str, run_names: Sequence[str]) -> Path | None:
    """按钉住的 run 对定位某场的逐调用产物；找不到返回 None（记为数据缺口）。"""
    for run_name in run_names:
        candidate = corpora.PUBLIC_RESULTS / run_name / f"{case_id}.responses.json"
        if candidate.is_file():
            return candidate
    return None


def chunk_partials(responses_path: Path) -> list[dict[str, Any]]:
    """从逐调用产物提取分段部分纪要；失败/非结构化的调用跳过（记数不静默）。"""
    payload = json.loads(responses_path.read_text(encoding="utf-8"))
    partials: list[dict[str, Any]] = []
    for call in payload.get("model_calls") or []:
        if call.get("call_type") != "MINUTES_CHUNK":
            continue
        output = call.get("parsed_output")
        if isinstance(output, Mapping) and any(output.get(field) for field in LIST_FIELDS):
            partials.append(dict(output))
    return partials


def build_union_output(partials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """分段并集伪纪要：五个列表字段按段序拼接，summary 按段序连排。

    这是合并步实际输入（分段 JSON 数组）的忠实并集——合并模型能看到的一切，
    一条不多一条不少。
    """
    union: dict[str, Any] = {
        "summary": "\n".join(str(p.get("summary") or "").strip()
                             for p in partials if str(p.get("summary") or "").strip()),
    }
    for field in LIST_FIELDS:
        union[field] = [item for p in partials for item in (p.get(field) or [])]
    return union


def _content_grams(text: str, n: int = 4) -> set[str]:
    """机械 4-gram 集合（本地实现，不 import 产品侧）。仅作交叉参照口径。"""
    compact = "".join(text.split())
    return {compact[i:i + n] for i in range(max(0, len(compact) - n + 1))}


def mechanical_coverage(keypoint_text: str, union: Mapping[str, Any]) -> float:
    """关键点的 4-gram 在并集全文中的重合率。**曾被作废口径，仅作参照。**"""
    haystack = _content_grams(json.dumps(union, ensure_ascii=False))
    needles = _content_grams(keypoint_text)
    if not needles:
        return 0.0
    return len(needles & haystack) / len(needles)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keypoints-packet", type=Path, required=True,
                        help="完整性 v3 产物（提供清单与 baseline 判定，保证同一把尺）")
    parser.add_argument("--output", type=Path,
                        default=RESULTS_DIR / "stage_attribution_test26.json")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--provider", default="default",
                        choices=sorted(model_client.PROVIDERS),
                        help="并集覆盖的裁判族；必须与 v3 包同族才有可比性")
    parser.add_argument("--authorized-llm-calls", type=int, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.authorized_llm_calls < 1:
        print("拒绝执行：--authorized-llm-calls 必须 ≥ 1", file=sys.stderr)
        return 2

    packet = json.loads(args.keypoints_packet.read_text(encoding="utf-8"))
    keypoints_by_case = {r["case_id"]: r.get("keypoints") or [] for r in packet.get("records") or []}
    # v3 包里同尺度的 baseline（合并后）逐点判定，直接复用不重复付费
    baseline_judged = {r["case_id"]: (r.get("arms") or {}).get("baseline")
                       for r in packet.get("records") or []}

    cases = corpora.load_new_contract_cases()
    if args.case_id:
        cases = [c for c in cases if c["case_id"] in set(args.case_id)]

    state: dict[str, Any] = {"records": []}
    if args.output.exists():
        try:
            state = json.loads(args.output.read_text(encoding="utf-8"))
        except ValueError:
            pass
    done = {r["case_id"] for r in state["records"] if r.get("union_judgment")}
    calls = 0

    for case in cases:
        case_id = case["case_id"]
        if case_id in done:
            print(f"跳过 {case_id}（已完成）")
            continue
        keypoints = keypoints_by_case.get(case_id) or []
        responses_path = find_responses_path(case_id, corpora.NEW_CONTRACT_TEST26_RUNS)
        record: dict[str, Any] = {"case_id": case_id}
        if not keypoints or responses_path is None:
            record["error"] = "缺清单或逐调用产物"
            state["records"].append(record)
            continue
        partials = chunk_partials(responses_path)
        union = build_union_output(partials)
        record["chunk_count"] = len(partials)
        record["single_chunk_control"] = len(partials) <= 1
        # 机械参照（作废口径，仅方向核对）：未覆盖点在并集里的 4-gram 重合
        judged = baseline_judged.get(case_id) or {}
        per_point = judged.get("per_keypoint") or []
        record["mechanical_reference"] = {
            "uncovered_points_4gram_in_union": [
                {"id": p.get("id"),
                 "gram_coverage": round(mechanical_coverage(str(p.get("text") or ""), union), 3)}
                for p in per_point if p.get("covered") is False
            ],
            "disclaimer": "机械 4-gram 口径曾因机制与端点共用同一机械被作废，仅作方向参照",
        }
        if calls + 1 > args.authorized_llm_calls:
            print(f"停止：授权已用尽（{calls}/{args.authorized_llm_calls}）", file=sys.stderr)
            state["records"].append(record)
            break
        started = time.monotonic()
        try:
            judgment = completeness.judge_coverage(case["transcript"], keypoints, union,
                                                   provider=args.provider)
            judgment["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            record["union_judgment"] = judgment
        except model_client.ModelClientError as error:
            record["union_judgment"] = {"error": str(error)[:200]}
        calls += 1
        # 同尺对比：合并后（baseline）覆盖来自 v3 包，不重复判定
        base_cov = (judged.get("coverage") or {})
        union_cov = (record["union_judgment"].get("coverage") or {})
        if base_cov.get("rate") is not None and union_cov.get("rate") is not None:
            record["attribution"] = {
                "union_coverage": union_cov["rate"],
                "merged_coverage": base_cov["rate"],
                "extraction_loss": 1.0 - union_cov["rate"],
                "merge_loss": union_cov["rate"] - base_cov["rate"],
            }
        state["records"].append(record)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {case_id}：分段 {len(partials)}，并集判定完成（调用累计 {calls}）")
        time.sleep(0.2)

    # 汇总：多块会议与单块对照分开报
    multi = [r for r in state["records"] if r.get("attribution") and not r.get("single_chunk_control")]
    single = [r for r in state["records"] if r.get("attribution") and r.get("single_chunk_control")]
    def _mean(rows: list[dict], key: str) -> float | None:
        values = [r["attribution"][key] for r in rows]
        return sum(values) / len(values) if values else None
    state["summary"] = {
        "multi_chunk_meetings": {
            "count": len(multi),
            "mean_union_coverage": _mean(multi, "union_coverage"),
            "mean_merged_coverage": _mean(multi, "merged_coverage"),
            "mean_extraction_loss": _mean(multi, "extraction_loss"),
            "mean_merge_loss": _mean(multi, "merge_loss"),
        },
        "single_chunk_controls": {
            "count": len(single),
            "mean_merge_loss": _mean(single, "merge_loss"),
            "note": "单块无合并步，merge_loss 应≈0；明显偏离说明判定噪声量级",
        },
        "provider": args.provider,
        "disclaimer": "诊断产物：不设阈值、不进 LEDGER 结论表；机械 4-gram 仅作方向参照（曾被作废口径）",
    }
    args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
