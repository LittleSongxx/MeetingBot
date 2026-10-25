"""裁判抽检：从轨迹/问答产物抽样生成回填表，再算 Cohen's kappa（**零模型调用**）。

## 为什么单列

单裁判读数只是方向信号（评测宪法第 2 条）：要进产品决策，必须有人工锚点。
本脚不做判定，只做两件事：

1. `--artifact ... --sample 20 --output x.csv`：从 `judges/rag.py --ask` 产物里
   **确定性地抽 N 条**（按 question 去重、固定 seed 打乱），生成一张 CSV 回填表。
   判定标签列（`judge_label` / `human_label`）**留空**，由裁判报告与人工分别回填。
2. `--score x.csv --output x.score.json`：读回填后的 CSV，算
   **Cohen's kappa**、逐条一致率（带 Clopper-Pearson 95% 区间）、混淆矩阵与逐条一致情况。

## 标签口径

`judge_label`/`human_label` 用同一套取值；`--score` 会做大小写/常见同义归一：
`1/true/yes/y/grounded/supported/支持/有依据/通过/是` → `yes`；
`0/false/no/n/ungrounded/unsupported/不支持/无依据/不通过/否` → `no`；
其他非空字符串按原样（小写）当类别。两列**都为空**的行跳过并单列计数，
只在两列都有值的行上算一致率与 kappa——**无回填数据时如实报，不报 0**。

kappa 解释用 Landis & Koch (1977) 的常用分档（<0 poor / 0–0.2 slight /
0.2–0.4 fair / 0.4–0.6 moderate / 0.6–0.8 substantial / 0.8–1 almost perfect）。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVALUATION_DIR = Path(__file__).resolve().parents[1]
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from lib import stats  # noqa: E402

DEFAULT_SAMPLE = 20
DEFAULT_SEED = 20261019
DEFAULT_CSV = EVALUATION_DIR / "results" / "judge_spotcheck.csv"
CSV_FIELDS = (
    "case_id", "question", "answer", "citations", "stop_reason",
    "judge_label", "human_label", "notes", "source_artifact",
)
LABEL_COLUMNS = ("judge_label", "human_label")

_POSITIVE = {"1", "true", "t", "yes", "y", "grounded", "supported",
             "支持", "有依据", "通过", "是"}
_NEGATIVE = {"0", "false", "f", "no", "n", "ungrounded", "unsupported",
             "不支持", "无依据", "不通过", "否"}


# --------------------------------------------------------------------------- #
# 抽样与 CSV
# --------------------------------------------------------------------------- #


def load_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def citation_cell(citations: Sequence[Mapping[str, Any]] | None) -> str:
    """把引用快照压成 CSV 单元格：保留 ref 与 quote 足够人工判断是否支撑答案。"""
    compact = []
    for citation in citations or []:
        if not isinstance(citation, Mapping):
            continue
        compact.append({
            "ref": citation.get("ref"),
            "kind": citation.get("kind"),
            "quote": citation.get("quote") or citation.get("source_text"),
        })
    return json.dumps(compact, ensure_ascii=False)


def collect_rows(artifacts: Iterable[tuple[str, Mapping[str, Any]]]) -> list[dict[str, str]]:
    """按 question 去重收集候选行（同一问题多份产物只取第一次出现的 turn）。"""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, artifact in artifacts:
        for turn in (artifact or {}).get("turns") or []:
            if not isinstance(turn, Mapping):
                continue
            question = str(turn.get("question") or "")
            if not question or question in seen:
                continue
            seen.add(question)
            rows.append({
                "case_id": str(turn.get("case_id") or ""),
                "question": question,
                "answer": str(turn.get("answer") or ""),
                "citations": citation_cell(turn.get("citations")),
                "stop_reason": str(turn.get("stop_reason") or ""),
                "judge_label": "",
                "human_label": "",
                "notes": "",
                "source_artifact": source,
            })
    rows.sort(key=lambda row: (row["case_id"], row["question"]))
    return rows


def sample_rows(rows: Sequence[Mapping[str, str]], sample: int,
                seed: int = DEFAULT_SEED) -> list[dict[str, str]]:
    """固定 seed 的确定性抽样：先排序再打乱，重复运行结果一致。"""
    if sample < 1:
        raise ValueError("sample 必须 >= 1")
    ordered = [dict(row) for row in rows]
    ordered.sort(key=lambda row: (row["case_id"], row["question"]))
    random.Random(seed).shuffle(ordered)
    return ordered[:sample]


def write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """写 UTF-8-SIG CSV（Excel 直接打开中文不乱码）；标签列留空。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in CSV_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV 缺列：{missing}")
        return [dict(row) for row in reader]


# --------------------------------------------------------------------------- #
# 一致性与 kappa
# --------------------------------------------------------------------------- #


def normalize_label(value: Any) -> str:
    """归一判定标签；空值返回空串（表示未回填）。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in _POSITIVE:
        return "yes"
    if text in _NEGATIVE:
        return "no"
    return text


def cohen_kappa(judge_labels: Sequence[Any], human_labels: Sequence[Any]) -> dict[str, Any] | None:
    """Cohen's kappa：观察一致率与期望一致率之差，按机会水平归一。"""
    if len(judge_labels) != len(human_labels):
        raise ValueError("两个标签序列长度必须一致")
    pairs = [(normalize_label(judge), normalize_label(human))
             for judge, human in zip(judge_labels, human_labels, strict=False)]
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for judge, human in pairs if judge == human) / n
    judge_counts = Counter(judge for judge, _ in pairs)
    human_counts = Counter(human for _, human in pairs)
    categories = sorted(set(judge_counts) | set(human_counts))
    expected = sum(judge_counts.get(category, 0) / n * human_counts.get(category, 0) / n
                   for category in categories)
    kappa = ((observed - expected) / (1.0 - expected)) if expected < 1.0 else None
    return {
        "n": n,
        "observed_agreement": observed,
        "expected_agreement": expected,
        "cohen_kappa": kappa,
        "categories": categories,
    }


def kappa_interpretation(value: float | None) -> str:
    """Landis & Koch (1977) 分档；None 表示不可定义。"""
    if value is None:
        return "不可定义（标签无变异或没有可评行）"
    if value < 0:
        return "poor（低于机会一致）"
    if value < 0.20:
        return "slight"
    if value < 0.40:
        return "fair"
    if value < 0.60:
        return "moderate"
    if value < 0.80:
        return "substantial"
    return "almost perfect"


def _rate(successes: int, total: int) -> dict[str, Any]:
    report = stats.proportion_report(int(successes), int(total))
    return {
        "successes": report["successes"],
        "total": report["total"],
        "rate": report["rate"],
        "clopper_pearson_95": report["clopper_pearson_95"],
        "wilson_95": report["wilson_95"],
    }


def score_rows(rows: Sequence[Mapping[str, Any]], *, csv_path: str = "") -> dict[str, Any]:
    """读回填行算 kappa；没有两列都填的行时如实报「无回填数据」。"""
    scored = []
    partially_filled = 0
    for index, row in enumerate(rows):
        judge = normalize_label(row.get("judge_label"))
        human = normalize_label(row.get("human_label"))
        if not judge and not human:
            continue
        if not judge or not human:
            partially_filled += 1
            continue
        scored.append({
            "row": index + 1,
            "case_id": str(row.get("case_id") or ""),
            "question": str(row.get("question") or ""),
            "judge_label": judge,
            "human_label": human,
            "agree": judge == human,
        })
    base = {
        "csv": csv_path,
        "n_rows": len(rows),
        "n_scored": len(scored),
        "n_partially_filled": partially_filled,
        "zero_model_calls": True,
    }
    if not scored:
        return {
            **base,
            "cohen_kappa": None,
            "observed_agreement": None,
            "reading": ("无回填数据：judge_label 与 human_label 同时非空的行数为 0。"
                        "先把两列都填上（同一套标签取值），再跑 --score；"
                        "本脚本**不**用空标签报 0 或 1。"),
        }
    kappa = cohen_kappa([row["judge_label"] for row in scored],
                        [row["human_label"] for row in scored]) or {}
    agreements = sum(1 for row in scored if row["agree"])
    confusion: dict[str, dict[str, int]] = {}
    for row in scored:
        bucket = confusion.setdefault(row["human_label"], {})
        bucket[row["judge_label"]] = bucket.get(row["judge_label"], 0) + 1
    return {
        **base,
        "cohen_kappa": kappa.get("cohen_kappa"),
        "kappa_interpretation": kappa_interpretation(kappa.get("cohen_kappa")),
        "observed_agreement": _rate(agreements, len(scored)),
        "expected_agreement": kappa.get("expected_agreement"),
        "n_agree": agreements,
        "n_disagree": len(scored) - agreements,
        "labels": {
            "judge": dict(Counter(row["judge_label"] for row in scored)),
            "human": dict(Counter(row["human_label"] for row in scored)),
        },
        "confusion": {human: dict(sorted(counts.items()))
                      for human, counts in sorted(confusion.items())},
        "per_row": scored,
        "reading": ("Cohen's kappa 只看两列是否一致，不判断哪一方对；"
                    "一致率区间用 Clopper-Pearson 95%（Wilson 一并给）。"
                    "kappa 分档为 Landis & Koch (1977)：<0 poor / 0–0.2 slight / "
                    "0.2–0.4 fair / 0.4–0.6 moderate / 0.6–0.8 substantial / "
                    "0.8–1 almost perfect。单次 20 条抽检只作方向信号，样本量必须一起报。"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _generate(args: argparse.Namespace) -> int:
    if not args.artifact:
        print(json.dumps({
            "sample": 0,
            "reading": ("无样本：未提供 --artifact（judges/rag.py --ask 产物）。"
                        "本脚本零模型调用，不会自己生成问答。"),
            "zero_model_calls": True,
        }, ensure_ascii=False, indent=2))
        return 0
    artifacts = []
    for path in args.artifact:
        try:
            artifacts.append((path.name, load_artifact(path)))
        except (OSError, ValueError) as error:
            print(f"读不到产物：{path}（{error}）", file=sys.stderr)
            return 2
    candidates = collect_rows(artifacts)
    if not candidates:
        print(json.dumps({
            "sample": 0,
            "candidates": 0,
            "reading": ("无样本：产物里没有带 question 的 turns，无法抽样。"
                        "核对是否为 judges/rag.py --ask 的 artifact_version=1 产物。"),
            "zero_model_calls": True,
        }, ensure_ascii=False, indent=2))
        return 0
    sampled = sample_rows(candidates, args.sample, args.seed)
    output = args.output or DEFAULT_CSV
    write_csv(output, sampled)
    summary = {
        "sample": len(sampled),
        "candidates": len(candidates),
        "sample_requested": args.sample,
        "seed": args.seed,
        "csv": str(output),
        "label_columns_left_empty": list(LABEL_COLUMNS),
        "zero_model_calls": True,
        "reading": ("已生成确定性抽样回填表（固定 seed）。请分别回填 judge_label（裁判侧）"
                    "与 human_label（人工侧，建议先不看裁判标签），再用 "
                    f"--score {output} --output <json> 计算 Cohen's kappa 与逐条一致率。"
                    "空标签行不参与统计，不报 0。"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _score(args: argparse.Namespace) -> int:
    try:
        rows = read_csv(args.score)
    except (OSError, ValueError) as error:
        print(f"读不到回填 CSV：{args.score}（{error}）", file=sys.stderr)
        return 2
    report = score_rows(rows, csv_path=str(args.score))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="裁判抽检：抽样生成回填 CSV / 读回填 CSV 算 Cohen's kappa（零模型调用）")
    parser.add_argument("--artifact", type=Path, action="append", default=None,
                        help="rag.py --ask 产物；可重复传（按 question 去重后抽样）")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help=f"抽样条数（默认 {DEFAULT_SAMPLE}）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"抽样随机种子（默认 {DEFAULT_SEED}，保证可复现）")
    parser.add_argument("--output", type=Path, default=None,
                        help="抽样模式：CSV 路径；评分模式：JSON 路径")
    parser.add_argument("--score", type=Path, default=None,
                        help="读回填后的 CSV，算 Cohen's kappa 与逐条一致率")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.score is not None:
        return _score(args)
    if args.sample < 1:
        print("--sample 必须 >= 1", file=sys.stderr)
        return 2
    return _generate(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CSV_FIELDS", "LABEL_COLUMNS", "DEFAULT_SAMPLE", "DEFAULT_SEED", "DEFAULT_CSV",
    "load_artifact", "citation_cell", "collect_rows", "sample_rows", "write_csv",
    "read_csv", "normalize_label", "cohen_kappa", "kappa_interpretation",
    "score_rows", "main",
]
