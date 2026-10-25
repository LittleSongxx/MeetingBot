"""MOSS-Transcribe-Diarize 直连侧车 8 场 cpCER 复测（绕开产品后端重启）。

链路（**直连侧车，非产品 HTTP 真实链路**）：
    ffmpeg 8 通道远场 → 单声道 16k wav（与产品 hyp_*_avg.mp3 同一混音口径）
    → POST http://127.0.0.1:9970/transcribe（scripts/moss_asr_server.py）
    → segments[{start,end,speaker,text}]
    → 转 task JSON（meeting_title 含 R8001_M8004；speaker_label="01-S01"；
       segment_no 顺序号）
    → evaluation/asr/cpcor.py::compute()（meeteval cpwer + cer_tool，与 c3600 同口径）

用法（必须用装了 meeteval 的 evaluation/.venv 解释器）：
    # 单场（驱动脚本逐场调用；每场前可重启侧车避免长音频显存碎片）
    evaluation/.venv/bin/python scripts/moss_direct_cpcor.py \
        --meeting R8001_M8004 --sidecar-url http://127.0.0.1:9970
    # 汇总（读 out-dir 下 single_*.json，生成 8 场宏平均 + 与 c3600 逐场配对）
    evaluation/.venv/bin/python scripts/moss_direct_cpcor.py --aggregate-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "evaluation"))
from asr.cpcor import compute  # noqa: E402 - 需先注入 sys.path 才能导入 evaluation 下的包

FIRST_TEST = REPO_ROOT / "evaluation" / "asr" / "first_test"
C3600_REPORT = FIRST_TEST / "cpcor" / "cpcor_report_c3600_8m.json"
MEETINGS = ["R8001_M8004", "R8003_M8001", "R8007_M8010", "R8007_M8011",
            "R8008_M8013", "R8009_M8018", "R8009_M8019", "R8009_M8020"]
DEFAULT_AUDIO_DIR = Path("/tmp/moss_direct_audio")
DEFAULT_OUT_DIR = FIRST_TEST / "moss_direct"


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def speaker_label(raw) -> str:
    """侧车 speaker 可能是 MOSS 原生 'S01'，也可能是数字 id；统一成 01-Sxx 标签。"""
    text = str(raw)
    if re.fullmatch(r"S\d+", text):
        return f"01-{text}"
    if re.fullmatch(r"\d+", text):
        return f"01-S0{int(text) + 1}"
    return f"01-{text}"


def post_transcribe(url: str, audio: Path, max_new_tokens: int, timeout: int = 3600) -> dict:
    """urllib 手工拼 multipart，避免额外依赖请求库。"""
    boundary = uuid.uuid4().hex
    audio_bytes = audio.read_bytes()
    parts = [
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
         "Content-Type: audio/wav\r\n\r\n").encode() + audio_bytes + b"\r\n",
        (f"--{boundary}\r\n"
         'Content-Disposition: form-data; name="max_new_tokens"\r\n\r\n'
         f"{max_new_tokens}\r\n").encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    request = urllib.request.Request(
        url.rstrip("/") + "/transcribe", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    payload["client_elapsed_seconds"] = round(time.monotonic() - started, 1)
    return payload


def to_task_json(meeting: str, payload: dict, audio_seconds: float,
                 max_new_tokens: int, tag: str) -> dict:
    segments = []
    for _index, seg in enumerate(payload.get("segments") or [], start=1):
        content = str(seg.get("text") or "").strip()
        if not content:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start)
        segments.append({
            "segment_no": len(segments) + 1,
            "start_ms": int(round(start * 1000)),
            "end_ms": int(round(end * 1000)),
            "speaker_label": speaker_label(seg.get("speaker")),
            "speaker_user_id": None,
            "speaker_name": None,
            "original_text": content,
            "content": content,
            "revised_by": None,
            "reviser_name": None,
            "revised_time": None,
        })
    return {
        "meeting_title": f"[ASR_EVAL] AliMeeting 远场首测 {meeting} ({tag})",
        "material_name": f"{meeting}.wav",
        "language": "zh",
        "status": "SUCCEEDED",
        "stage": "转写完成",
        "progress": 100,
        "model_name": payload.get("model"),
        "engine": "moss-direct-sidecar",
        "engine_note": "直连 scripts/moss_asr_server.py 侧车，非产品 HTTP 真实链路",
        "audio_seconds": audio_seconds,
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "max_new_tokens": max_new_tokens,
        "truncated": payload.get("truncated"),
        "segments": segments,
    }


def run_one(meeting: str, args: argparse.Namespace) -> dict:
    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    audio = Path(args.audio_dir) / f"{meeting}.wav"
    if not audio.exists():
        raise SystemExit(f"缺音频 {audio}（先跑 scripts/prepare_moss_audio.sh）")
    seconds = wav_seconds(audio)
    tokens = args.max_new_tokens or int(seconds * args.token_rate + args.token_margin)
    print(f"[direct] {meeting}: {seconds:.1f}s audio, max_new_tokens={tokens}", flush=True)
    try:
        payload = post_transcribe(args.sidecar_url, audio, tokens, args.http_timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:2000]
        record = {"meeting": meeting, "status": "FAILED", "stage": "sidecar_http",
                  "error": f"HTTP {error.code}: {detail}", "audio_seconds": seconds}
        (out_dir / f"single_{meeting}.{args.tag}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[direct] {meeting}: FAILED {record['error'][:300]}", flush=True)
        return record
    (raw_dir / f"{meeting}.raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    task = to_task_json(meeting, payload, seconds, tokens, args.tag)
    task_path = FIRST_TEST / f"task_{meeting}.{args.tag}.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    record = {
        "meeting": meeting,
        "status": "SUCCEEDED",
        "task": str(task_path.relative_to(REPO_ROOT)),
        "audio_seconds": round(seconds, 1),
        "covered_seconds": round(float(payload.get("duration") or 0), 1),
        "truncated": bool(payload.get("truncated")),
        "n_segments": len(task["segments"]),
        "hyp_speakers": len({s["speaker_label"] for s in task["segments"]}),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "client_elapsed_seconds": payload.get("client_elapsed_seconds"),
        "max_new_tokens": tokens,
        "raw_json": str((raw_dir / f"{meeting}.raw.json").relative_to(REPO_ROOT)),
    }
    try:
        metrics = compute(task_path, Path(args.python_bin), out_dir / "cpcer")
        record.update({
            "cpcer": metrics["cpcor"],
            "cer_no_punct": metrics["cer_no_punct"],
            "delta_cp": metrics["delta_cp"],
            "ref_speakers": metrics["ref_speakers"],
            "meeteval_raw": metrics["meeteval_raw"],
        })
    except Exception as error:  # noqa: BLE001 - 指标失败要落盘根因，不吞
        record.update({"status": "METRIC_FAILED", "error": f"{type(error).__name__}: {error}"})
    (out_dir / f"single_{meeting}.{args.tag}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[direct] {meeting}: {json.dumps(record, ensure_ascii=False)[:600]}", flush=True)
    return record


def aggregate(args: argparse.Namespace) -> dict:
    out_dir = Path(args.out_dir)
    c3600 = {row["session"]: row for row in
             json.loads(C3600_REPORT.read_text(encoding="utf-8"))["meetings"]}
    rows = []
    for meeting in MEETINGS:
        single = out_dir / f"single_{meeting}.{args.tag}.json"
        if not single.exists():
            rows.append({"session": meeting, "status": "MISSING"})
            continue
        record = json.loads(single.read_text(encoding="utf-8"))
        row = {"session": meeting, "status": record.get("status"),
               "cpcer": record.get("cpcer"), "cer_no_punct": record.get("cer_no_punct"),
               "delta_cp": record.get("delta_cp"),
               "audio_seconds": record.get("audio_seconds"),
               "covered_seconds": record.get("covered_seconds"),
               "truncated": record.get("truncated"),
               "elapsed_seconds": record.get("elapsed_seconds"),
               "n_segments": record.get("n_segments"),
               "hyp_speakers": record.get("hyp_speakers")}
        baseline = c3600.get(meeting)
        if baseline and record.get("cpcer") is not None:
            row["c3600_cpcer"] = baseline["cpcor"]
            row["delta_pp_vs_c3600"] = round((baseline["cpcor"] - record["cpcer"]) * 100, 2)
            row["relative_vs_c3600"] = (round((baseline["cpcor"] - record["cpcer"])
                                              / baseline["cpcor"] * 100, 1)
                                        if baseline["cpcor"] else None)
        rows.append(row)
    valid = [r for r in rows if r.get("cpcer") is not None]
    macro = {}
    if valid:
        macro = {
            "n_valid": len(valid),
            "n_total": len(MEETINGS),
            "cpcer": round(sum(r["cpcer"] for r in valid) / len(valid), 4),
            "cer_no_punct": round(sum(r["cer_no_punct"] for r in valid) / len(valid), 4),
            "delta_cp": round(sum(r["delta_cp"] for r in valid) / len(valid), 4),
            "macro_delta_pp_vs_c3600": round(
                sum(r["delta_pp_vs_c3600"] for r in valid if "delta_pp_vs_c3600" in r)
                / len([r for r in valid if "delta_pp_vs_c3600" in r]), 2),
            "truncated_meetings": [r["session"] for r in valid if r.get("truncated")],
        }
        c3600_subset = [c3600[r["session"]]["cpcor"] for r in valid if r["session"] in c3600]
        if c3600_subset:
            macro["c3600_macro_same_subset"] = round(sum(c3600_subset) / len(c3600_subset), 4)
    report = {
        "condition": args.tag,
        "link": "direct-sidecar（直连 scripts/moss_asr_server.py，非产品 HTTP 真实链路）",
        "audio": "AliMeeting Eval far-field 8ch → ffmpeg -ac 1 -ar 16000（与产品 hyp_*_avg.mp3 同混音口径）",
        "meetings": rows,
        "macro": macro,
        "c3600_reference": "evaluation/asr/first_test/cpcor/cpcor_report_c3600_8m.json",
    }
    path = out_dir / f"cpcor_report_{args.tag}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"[direct] aggregate → {path}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meeting", default=None, help="单场会议 id，如 R8001_M8004")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="只汇总已落盘的 single_*.json（不请求侧车）")
    parser.add_argument("--sidecar-url", default="http://127.0.0.1:9970")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--tag", default="moss_direct")
    parser.add_argument("--token-rate", type=float, default=16.0,
                        help="每秒钟音频预估 text token 数（实测 14~15，取 16 留余量）")
    parser.add_argument("--token-margin", type=int, default=6144,
                        help="max_new_tokens 额外余量（含首尾标签开销）")
    parser.add_argument("--max-new-tokens", type=int, default=None,
                        help="显式覆盖 max_new_tokens（消融用）")
    parser.add_argument("--http-timeout", type=int, default=3600)
    parser.add_argument("--python-bin",
                        default=str(REPO_ROOT / "evaluation" / ".venv" / "bin" / "python"))
    args = parser.parse_args()
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    if args.aggregate_only:
        aggregate(args)
        return 0
    if not args.meeting:
        parser.error("需要 --meeting 或 --aggregate-only")
    record = run_one(args.meeting, args)
    aggregate(args)
    return 0 if record.get("cpcer") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
