"""cpCER 三件套：CER / cpCER / Δcp（W2 仪器，2026-09-27 建）。

口径（对齐 M2MeT 官方惯例与 MeetEval 实操调研）：
- 参考 = AliMeeting Eval 近讲话戴 TextGrid（**每说话人一个文件**，说话人身份即
  文件名）。近/远场共享同一套近讲来源转写——M2MeT 官方 cpCER 评的就是它，
  系统性高估远场错误是口径属性（文献公认），不是我们的口径错误。
- 假设 = 产品转写任务记录（task JSON 的 segments，带 speaker_label）。
- 指标：CER（单说话人拼接，cer_tool 同口径交叉验证）+ cpCER（MeetEval cpwer，
  汉字逐字空格切分后计算——数值等价字级 cpCER，绕开 CJK 分词 issue）+
  Δcp = cpCER − CER（说话人归属损失的单独度量）。
- 归一化：ref/hyp 双侧统一走 cer_tool 的 no_punct 口径（去空白+去标点），
  逐字空格切分。cpCER 不需要时间对齐（STM 时间字段为占位）。

用法（零模型调用）：
    evaluation/.venv/bin/python evaluation/asr/cpcor.py \
        --task evaluation/asr/first_test/task_R8003_M8001.json
    # 或批量：--all-first-test
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIRST_TEST = HERE / "first_test"
AUDIO_ROOT = FIRST_TEST / "audio" / "Eval_Ali"
NEAR_TG = AUDIO_ROOT / "Eval_Ali_near" / "textgrid_dir"

INTERVAL = re.compile(
    r"intervals\s*\[(\d+)\]:\s*xmin\s*=\s*([0-9.]+)\s*xmax\s*=\s*([0-9.]+)\s*text\s*=\s*\"((?:[^\"\\]|\\.)*)\"")

# 与 cer_tool 的 no_punct 口径一致（评测侧独立持有一份，见 DESIGN §5 隔离边界）
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]")


def normalize_chars(text: str) -> str:
    """去空白 → 去标点 → 逐字空格切分（MeetEval 的字级 token）。"""
    stripped = _PUNCT.sub("", _WS.sub("", text or ""))
    return " ".join(stripped)


def parse_textgrid(path: Path) -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []
    for match in INTERVAL.finditer(path.read_text(encoding="utf-8")):
        text = match.group(4).replace('\\"', '"').strip()
        if text and text not in {"", " ", "sil", "SP-IL", "SP-IN"}:
            rows.append((float(match.group(2)), text))
    return rows


def reference_time_ordered(meeting: str) -> str:
    """全部说话人的区间按时间排序拼接（单说话人口径 CER 用，交叉验证 cer_tool）。"""
    rows: list[tuple[float, str]] = []
    for path in sorted(NEAR_TG.glob(f"{meeting}_N_SPK*.TextGrid")):
        rows.extend(parse_textgrid(path))
    rows.sort(key=lambda r: r[0])
    return "".join(text for _s, text in rows)


def reference_stm(meeting: str) -> list[str]:
    """近场 TextGrid → STM 行（每说话人一行拼接文本，时间字段占位）。"""
    lines: list[str] = []
    for path in sorted(NEAR_TG.glob(f"{meeting}_N_SPK*.TextGrid")):
        speaker = path.stem.split("_N_")[-1]
        rows = parse_textgrid(path)
        rows.sort(key=lambda r: r[0])
        words = normalize_chars("".join(text for _s, text in rows))
        if words:
            lines.append(f"{meeting} 1 {speaker} 0 1 {words}")
    return lines


def hypothesis_stm(task_json: Path) -> tuple[str, list[str]]:
    """产品任务记录 → STM 行（按 speaker_label 分组拼接；标签即说话人 ID）。"""
    data = json.loads(task_json.read_text(encoding="utf-8"))
    meeting = re.search(r"(R\d+_M\d+)", data.get("meeting_title") or task_json.stem)
    session = meeting.group(1) if meeting else task_json.stem
    segments = sorted(data.get("segments") or [],
                      key=lambda s: int(s.get("segment_no") or 0))
    by_speaker: dict[str, list] = {}
    for seg in segments:
        label = str(seg.get("speaker_label") or "unknown")
        by_speaker.setdefault(label, []).append(seg)
    lines: list[str] = []
    for label in sorted(by_speaker):
        segs = sorted(by_speaker[label], key=lambda s: int(s.get("segment_no") or 0))
        words = normalize_chars("".join(str(s.get("content") or "") for s in segs))
        if words:
            # 说话人标签里的连字符换成下划线：STM 字段以空白分隔
            lines.append(f"{session} 1 {label.replace('-', '_')} 0 1 {words}")
    return session, lines


def run_meeteval(ref_path: Path, hyp_path: Path, python_bin: Path) -> dict:
    """调 meeteval-wer cpwer，读它落盘的 <stem>_cpwer.json（stdout 是人读摘要）。"""
    subprocess.run(
        [str(python_bin), "-m", "meeteval.wer", "cpwer",
         "-r", str(ref_path), "-h", str(hyp_path)],
        capture_output=True, text=True, check=True)
    return json.loads((ref_path.parent / (hyp_path.stem + "_cpwer.json")).read_text(encoding="utf-8"))


def compute(task: Path, python_bin: Path, workdir: Path) -> dict:
    session, hyp_lines = hypothesis_stm(task)
    _data = json.loads(task.read_text(encoding="utf-8"))
    segments = sorted(_data.get("segments") or [],
                      key=lambda s: int(s.get("segment_no") or 0))
    ref_lines = reference_stm(session)
    if not ref_lines or not hyp_lines:
        raise SystemExit(f"{session}: 参考或假设 STM 为空（ref={len(ref_lines)} hyp={len(hyp_lines)}）")
    workdir.mkdir(parents=True, exist_ok=True)
    ref_path, hyp_path = workdir / f"{session}.ref.stm", workdir / f"{session}.hyp.stm"
    ref_path.write_text("\n".join(ref_lines) + "\n", encoding="utf-8")
    hyp_path.write_text("\n".join(hyp_lines) + "\n", encoding="utf-8")
    report = run_meeteval(ref_path, hyp_path, python_bin)
    # meeteval 输出结构：{"cpwer": {"sessions": {...}, "overall": {...}}, ...}
    cp = report.get("cpwer") or report
    overall = cp.get("overall") or cp
    cpcor = (errors / length) if (errors := overall.get("errors")) is not None and (length := overall.get("length")) else None

    # 同口径 CER（时间序单说话人拼接）：交叉验证 cer_tool 的历史数字。
    # 不能用按说话人分组的串——分组序与历史口径（时间序）不一致，CER 会虚高。
    from asr.cer_tool import cer
    cer_value = cer(reference_time_ordered(session),
                    "".join(str(s.get("content") or "") for s in segments))

    out = {
        "session": session, "task": str(task),
        "ref_speakers": len(ref_lines), "hyp_speakers": len(hyp_lines),
        "cpcor": round(cpcor, 4) if cpcor is not None else None,
        "cer_no_punct": round(cer_value["no_punct"], 4),
        "delta_cp": round(cpcor - cer_value["no_punct"], 4) if cpcor is not None else None,
        "meeteval_raw": {"errors": overall.get("errors"), "length": overall.get("length"),
                        "falarm_speaker": overall.get("falarm_speaker"), "scored_speaker": overall.get("scored_speaker")},
    }
    print(json.dumps(out, ensure_ascii=False))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", type=Path, default=None, help="task JSON 路径")
    parser.add_argument("--all-first-test", action="store_true",
                        help="对 first_test 下全部主条件 task JSON 计算（不含变体后缀）")
    parser.add_argument("--python-bin", type=Path,
                        default=HERE.parent / ".venv" / "bin" / "python",
                        help="装了 meeteval 的解释器")
    parser.add_argument("--workdir", type=Path, default=FIRST_TEST / "cpcor")
    parser.add_argument("--output", type=Path, default=FIRST_TEST / "cpcor" / "cpcor_report.json")
    args = parser.parse_args()

    sys.path.insert(0, str(HERE.parent))
    if args.all_first_test:
        # 只取主条件（文件名无变体后缀）；变体（.c3600/.best_c3600）单独跑 --task
        import re as _re
        tasks = [t for t in sorted(FIRST_TEST.glob("task_R*_M*.json"))
                 if _re.fullmatch(r"task_R\d+_M\d+\.json", t.name)]
    else:
        tasks = [args.task] if args.task else []
    if not tasks:
        parser.error("需要 --task 或 --all-first-test")

    results = []
    for task in tasks:
        try:
            results.append(compute(task, args.python_bin, args.workdir))
        except SystemExit as error:
            print(f"skip {task.name}: {error}", file=sys.stderr)
    if results:
        macro_cpcor = sum(r["cpcor"] for r in results) / len(results)
        macro_cer = sum(r["cer_no_punct"] for r in results) / len(results)
        summary = {"meetings": results,
                   "macro": {"cpcor": round(macro_cpcor, 4),
                             "cer_no_punct": round(macro_cer, 4),
                             "delta_cp": round(macro_cpcor - macro_cer, 4)}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary["macro"], ensure_ascii=False), "→", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
