"""MOSS 自身质量指标汇总（LEDGER §45：MOSS 直接采用，不做 c3600 配对对照）。

数据源：evaluation/asr/first_test/moss_direct/single_<meeting>.moss_direct.json
（由 scripts/moss_direct_cpcor.py 直连侧车逐场产出；含 cpCER/CER/Δcp/覆盖率/耗时/token 等）

输出：
1) evaluation/results/moss_own_metrics.json —— 8 场自身指标 + 宏平均 + 有效/失败/截断统计
2) stdout 打印 Markdown 表（供 reports 正文直接引用）

用法：
    evaluation/.venv/bin/python scripts/moss_own_metrics.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOSS_DIR = REPO_ROOT / "evaluation" / "asr" / "first_test" / "moss_direct"
OUT_JSON = REPO_ROOT / "evaluation" / "results" / "moss_own_metrics.json"
MEETINGS = ["R8001_M8004", "R8003_M8001", "R8007_M8010", "R8007_M8011",
            "R8008_M8013", "R8009_M8018", "R8009_M8019", "R8009_M8020"]
TAG = "moss_direct"
# 覆盖率判截断门槛：>=99% 视为未静默截断（侧车 truncated 字段在 v1 用 -5s 绝对差，会把
# 尾部静音场误标；此处同时记录原始 flag 与覆盖口径，两者都报）
COVERAGE_OK = 0.99


def load(meeting: str) -> dict:
    path = MOSS_DIR / f"single_{meeting}.{TAG}.json"
    if not path.exists():
        return {"session": meeting, "status": "MISSING"}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    rows = []
    for meeting in MEETINGS:
        record = load(meeting)
        audio = record.get("audio_seconds")
        covered = record.get("covered_seconds")
        elapsed = record.get("elapsed_seconds")
        coverage = round(covered / audio, 4) if audio and covered else None
        row = {
            "session": meeting,
            "status": record.get("status", "MISSING"),
            "cpcer": record.get("cpcer"),
            "cer_no_punct": record.get("cer_no_punct"),
            "delta_cp": record.get("delta_cp"),
            "audio_seconds": audio,
            "covered_seconds": covered,
            "coverage": coverage,
            "truncated_flag": record.get("truncated"),
            "truncated_by_coverage": (coverage is not None and coverage < COVERAGE_OK),
            "hyp_speakers": record.get("hyp_speakers"),
            "ref_speakers": record.get("ref_speakers"),
            "n_segments": record.get("n_segments"),
            "elapsed_seconds": elapsed,
            "rtf": round(elapsed / audio, 3) if audio and elapsed else None,
            "max_new_tokens": record.get("max_new_tokens"),
            "reason": record.get("reason"),
        }
        rows.append(row)

    valid = [r for r in rows if r["cpcer"] is not None]
    failed = [r for r in rows if r["cpcer"] is None]
    macro = {}
    if valid:
        macro = {
            "n_valid": len(valid),
            "n_total": len(MEETINGS),
            "cpcer": round(sum(r["cpcer"] for r in valid) / len(valid), 4),
            "cer_no_punct": round(sum(r["cer_no_punct"] for r in valid) / len(valid), 4),
            "delta_cp": round(sum(r["delta_cp"] for r in valid) / len(valid), 4),
            "coverage": round(sum(r["coverage"] for r in valid) / len(valid), 4),
            "coverage_min": min(r["coverage"] for r in valid),
            "rtf": round(sum(r["rtf"] for r in valid) / len(valid), 3),
            "rtf_min": min(r["rtf"] for r in valid),
            "rtf_max": max(r["rtf"] for r in valid),
            "truncated_flag_count": sum(1 for r in valid if r["truncated_flag"]),
            "truncated_by_coverage_count": sum(1 for r in valid if r["truncated_by_coverage"]),
            "failed_count": len(failed),
            "total_audio_seconds": round(sum(r["audio_seconds"] for r in valid), 1),
            "total_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in valid), 1),
            "max_single_elapsed_seconds": max(r["elapsed_seconds"] for r in valid),
        }
    report = {
        "condition": "moss_direct",
        "policy": "LEDGER §45：MOSS 直接采用；本文件只报自身质量指标，不含相对 c3600 提升/逐场配对",
        "link": "direct-sidecar（直连 scripts/moss_asr_server.py，非产品 HTTP 真实链路）",
        "metric_def": {
            "cpcer": "MeetEval cpWER（字级 no_punct，汉字逐字空格切分；参考=AliMeeting Eval 近讲 TextGrid）",
            "cer_no_punct": "时间序单说话人拼接的 cer_tool no_punct CER",
            "delta_cp": "cpCER − CER（说话人归属损失；负值=归属对齐后错误低于单流）",
            "coverage": "最后一段 end / 音频时长（侧车 audio_seconds 与 duration）",
            "truncated_by_coverage": "coverage < 0.99 判静默截断（尾部静音造成的 <10s 差值不算）",
        },
        "meetings": rows,
        "macro": macro,
        "failures": [{"session": r["session"], "status": r["status"], "reason": r["reason"]} for r in failed],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {OUT_JSON.relative_to(REPO_ROOT)}")
    print()
    print("| 会议 | 音频min | cpCER | CER | Δcp(pp) | 覆盖率 | hyp/ref spk | 段数 | RTF | 耗时s | 截断 |")
    print("|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|")
    for r in rows:
        if r["cpcer"] is None:
            print(f"| {r['session']} | — | — | — | — | — | — | — | — | — | {r['status']} |")
            continue
        trunc = ("否" if not r["truncated_by_coverage"]
                 else f"**是**（cov {r['coverage']:.2%}）")
        if r["truncated_flag"] and not r["truncated_by_coverage"]:
            trunc = "否（尾部静音，flag 误标）"
        print("| {s} | {a:.1f} | **{cp:.2f}%** | {ce:.2f}% | {d:+.2f} | {cov:.2f}% | {h}/{rf} | {n} | {rtf:.3f} | {el:.1f} | {tr} |".format(
            s=r["session"], a=r["audio_seconds"] / 60, cp=r["cpcer"] * 100,
            ce=r["cer_no_punct"] * 100, d=r["delta_cp"] * 100, cov=r["coverage"] * 100,
            h=r["hyp_speakers"], rf=r["ref_speakers"], n=r["n_segments"], rtf=r["rtf"],
            el=r["elapsed_seconds"], tr=trunc))
    print()
    print("宏平均（n_valid={}/{}）：cpCER {:.2f}% / CER {:.2f}% / Δcp {:+.2f}pp / 覆盖率 {:.2f}%（min {:.2f}%）/ RTF {:.3f}（{:.3f}–{:.3f}） / 截断 {} 场（覆盖口径；侧车 flag {} 场）".format(
        macro.get("n_valid", 0), macro.get("n_total", len(MEETINGS)),
        macro.get("cpcer", 0) * 100, macro.get("cer_no_punct", 0) * 100,
        macro.get("delta_cp", 0) * 100, macro.get("coverage", 0) * 100,
        macro.get("coverage_min", 0) * 100, macro.get("rtf", 0),
        macro.get("rtf_min", 0), macro.get("rtf_max", 0),
        macro.get("truncated_by_coverage_count", 0), macro.get("truncated_flag_count", 0)))
    print("总音频 {:.1f} min，总推理墙钟 {:.1f} min，单场最长 {:.1f}s；失败 {} 场".format(
        macro.get("total_audio_seconds", 0) / 60, macro.get("total_elapsed_seconds", 0) / 60,
        macro.get("max_single_elapsed_seconds", 0), macro.get("failed_count", 0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
