"""ASR 首测第一步：从近讲话戴 TextGrid 构建**参考转写**（金标侧）。

近讲 TextGrid（每说话人一个文件，单 IntervalTier）→ 全部非空区间按时间排序拼接
（跨说话人交错按 xmin 排序）→ 会议级参考文本。无说话人标签（CER 不需要；
cpCER/cpWER 需要说话人对齐，属 MeetEval 后续工作，如实声明）。

 punctuation：TextGrid 原文带标点——cer_tool 的三口径正好覆盖"标点实验"。

用法：
    python evaluation/asr/first_test/build_reference.py --meeting R8001_M8004
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

AUDIO_ROOT = Path(__file__).resolve().parent / "audio" / "Eval_Ali"
INTERVAL = re.compile(
    r"intervals\s*\[(\d+)\]:\s*xmin\s*=\s*([0-9.]+)\s*xmax\s*=\s*([0-9.]+)\s*text\s*=\s*\"((?:[^\"\\]|\\.)*)\"")


def parse_textgrid(path: Path) -> list[tuple[float, str]]:
    """单层 IntervalTier 的 (xmin, text) 列表。"""
    content = path.read_text(encoding="utf-8")
    rows: list[tuple[float, str]] = []
    for match in INTERVAL.finditer(content):
        text = match.group(4).replace('\\"', '"').strip()
        if text and text not in {"", " ", "sil", "SP-IL", "SP-IN"}:
            rows.append((float(match.group(2)), text))
    return rows


def build_reference(meeting: str) -> str:
    near_tg = AUDIO_ROOT / "Eval_Ali_near" / "textgrid_dir"
    rows: list[tuple[float, str]] = []
    for path in sorted(near_tg.glob(f"{meeting}_N_SPK*.TextGrid")):
        rows.extend(parse_textgrid(path))
    rows.sort(key=lambda r: r[0])
    return "".join(text for _start, text in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting", required=True, help="如 R8001_M8004")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    reference = build_reference(args.meeting)
    out = args.output or Path(__file__).resolve().parent / f"reference_{args.meeting}.txt"
    out.write_text(reference, encoding="utf-8")
    print(f"{args.meeting}: 参考转写 {len(reference)} 字符 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
