"""轴5 检索侧指标：recall@k / MRR / nDCG@k（**零模型调用**）。

## 为什么单列一套

`judges/rag.py` 的四个指标都靠判定模型（换族、换思考开关都会动，实测见 LEDGER
的仪器复核）。检索侧这三个指标**不需要模型**：金标里已经有 `source.segments`
（这段答案出自转写第几号片段），而检索到的切片带时间窗——两边一映射就能算。

它们是"每次 `make smoke` 都能复查"的那一类，因此值得单独建：改切块、改召回、
加过滤或 rerank 之后，不用花钱就能看出检索有没有变好/变坏。

## 口径（与 judge 的那些不同，必须连口径一起报）

- **相关切片**：片段的起点落进切片的时间范围（`start_second ≤ 片段起点 ≤ end_second`，
  没有精确窗时用引文里的定位前缀 `[224s]` 的区间近似），且（当两侧都带来源身份时）
  切片与片段属于**同一段录音**。切片是转写窗口（几句一段），金标是句子级，
  所以"起点落进范围"是这里唯一可判定的关系。
- **跨录音问题**（金标里带 `also_in`）现在可以评了：切片引用带上了 `source_id`
  （来源录音 id），每个来源各按自己的片段表映射。缺 `source_id` 的老产物仍按"判不了"跳过。
- `recall@k`：金标片段里**被 top-k 覆盖**的比例（覆盖 = 至少一条相关切片进了前 k）。
  这里 k 取"这一次实际检索到的条数"（模型自选 limit，实测 1–10），因此它是
  "这次检索有没有盖住金标"，不是"固定 k 下的召回"——**报数时必须连 k 一起报**。
- `MRR`：第一条相关切片的排名的倒数（没有相关切片记 0）。
- `nDCG@k`：二值增益（相关=1）。IDCG 按"理想情况下前 min(k, 相关切片总数) 名全相关"算。

## 数据缺口（如实标注，不猜）

产物里必须有**检索到的全部切片**（含没被引用的那些）才能算这些指标；老产物只存了
"答案引用到的"那一部分。缺 `retrieved` 字段时，本模块**报"无样本"**而不是拿引用集冒充
检索集——那会把召回算高（分母里少了没被引用的东西）。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

EVALUATION_DIR = Path(__file__).resolve().parents[1]
# 切片正文里的定位前缀（`[224s][张晨] …`）：它既是页面上"点回原句"的锚点，
# 也是这里判"这条切片盖住了哪几秒"的对照物
_LOCATOR = re.compile(r"\[(\d{1,6})s\]")
DEFAULT_ARTIFACT = EVALUATION_DIR / "results" / "chat_answers.json"
DEFAULT_QA_SET = EVALUATION_DIR / "gold" / "qa_seed.json"


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    target = path or DEFAULT_ARTIFACT
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def segment_starts(sources: Mapping[str, Any], task_id: int) -> dict[int, int]:
    """片段号 → 起始秒（只取这个转写任务的片段）。

    产物里的 `source_texts[会议]` 形如 `{"task_ids": [...], "segments": [{task_id,
    segment_no, start_ms, ...}], "text": ...}`；老版抓取只有 `text`，
    那时这个函数返回空字典——调用方据此报"判不了"，不拿别的东西凑。
    """
    starts: dict[int, int] = {}
    for entry in (sources or {}).values():
        for segment in (entry.get("segments") or []):
            if int(segment.get("task_id") or 0) != int(task_id):
                continue
            if segment.get("segment_no") is None or segment.get("start_ms") is None:
                continue
            starts[int(segment["segment_no"])] = int(segment["start_ms"]) // 1000
    return starts


def _window(item: Mapping[str, Any]) -> tuple[int, int] | None:
    """这条切片覆盖的时间范围（秒）。判不了返回 None。

    两条来源，优先精确的那条：
    1. 引用自带的时间窗（`start_second` / `end_second`，新产物有）；
    2. **引文正文里的定位前缀**（`[224s][张晨]`）——切片每一行都带它，所以
       "这条盖住了哪几秒"从引文本身就能读出来（老产物只有这一条路）。

    两者都没有（纪要条目切片没有时间轴）→ 判不了，**不算覆盖，也不假装算过**。
    """
    start, end = item.get("start_second"), item.get("end_second")
    if start is not None and end is not None:
        return int(start), int(end)
    markers = [int(m) for m in _LOCATOR.findall(str(item.get("quote") or ""))]
    if markers:
        return min(markers), max(markers)
    return None


def _covers(item: Mapping[str, Any], start_second: int, *, task_id: int | None = None) -> bool:
    """这条检索切片覆盖这个片段吗：起点落进时间范围，且（能判时）来自同一段录音。

    来源身份（`source_id`）能判时必须一致：两段录音的时间轴都从 0 开始，
    只比时间窗会把"上午 51 秒"与"下午 51 秒"算成同一条——那是假命中。
    """
    window = _window(item)
    if not window or not (window[0] <= int(start_second) <= window[1]):
        return False
    source_id = item.get("source_id")
    if task_id is None or source_id is None:
        return True    # 判不了来源就不拿它当否决条件（老产物没有这个字段）
    return int(source_id) == int(task_id)


def case_sources(case: Mapping[str, Any]) -> list[tuple[int, list[int]]]:
    """这道题的金标来源：`source` 与可选的 `also_in`（跨录音题的第二段录音）。"""
    pairs: list[tuple[int, list[int]]] = []
    for key in ("source", "also_in"):
        entry = case.get(key) or {}
        task_id = entry.get("task_id")
        segments = [int(seg) for seg in (entry.get("segments") or [])]
        if task_id is not None and segments:
            pairs.append((int(task_id), segments))
    return pairs


def evaluate_turn(turn: Mapping[str, Any], case: Mapping[str, Any],
                  sources: Mapping[str, Any]) -> dict[str, Any] | None:
    """一轮的检索侧读数。判不了（缺检索集/缺片段时间）返回 None。

    `sources` 是产物的 `source_texts`（每个会议一条，内含各任务的片段→时间表）。
    金标可能横跨多段录音（`also_in`），这里按 (task_id, segment_no) 记账——
    **跨录音题因此可评**，前提是切片带 `source_id`（新产物有）。
    """
    retrieved = turn.get("retrieved")
    if not retrieved:
        return None
    gold: list[tuple[int, int, int]] = []      # (task_id, segment_no, start_second)
    for task_id, segments in case_sources(case):
        per_task = segment_starts(sources, task_id)
        for seg in segments:
            if int(seg) in per_task:
                gold.append((task_id, int(seg), per_task[int(seg)]))
    if not gold:
        return None
    relevant = [any(_covers(item, start, task_id=task) for task, _, start in gold)
                for item in retrieved]
    k = len(retrieved)
    covered = {(task, seg) for task, seg, start in gold
               if any(_covers(item, start, task_id=task) for item in retrieved)}
    # 来源对不上时的覆盖数是"能判的那部分"：来源身份缺失（老产物）按可判的算
    scorable = [entry for entry in gold if _source_known(retrieved, entry[0])]
    mrr = next((1.0 / (index + 1) for index, hit in enumerate(relevant) if hit), 0.0)
    dcg = sum((1.0 if hit else 0.0) / math.log2(index + 2) for index, hit in enumerate(relevant))
    ideal = sum(1.0 / math.log2(index + 2)
                for index in range(min(k, max(1, sum(1 for hit in relevant if hit)))))
    denominator = len(scorable) or len(gold)
    return {
        "case_id": case.get("id"),
        "k": k,
        "recall": len(covered) / denominator,
        "mrr": mrr,
        "ndcg": (dcg / ideal) if ideal else None,
        "relevant_count": sum(1 for hit in relevant if hit),
        "sources_known": bool(scorable),
    }


def _source_known(retrieved: list[Mapping[str, Any]], task_id: int) -> bool:
    """检索集里有没有带来源身份的切片（有才敢按来源判覆盖）。"""
    return any(item.get("source_id") is not None for item in retrieved)


def summarize(artifact: Mapping[str, Any] | None,
              qa_set: Mapping[str, Any] | None) -> dict[str, Any]:
    """逐 case 算，再宏平均（每 case 等权）。判不了的样本单独计数，不混进分母。"""
    turns = (artifact or {}).get("turns") or []
    by_question = {turn.get("question"): turn for turn in turns}
    sources = (artifact or {}).get("source_texts") or {}
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for case in (qa_set or {}).get("cases") or []:
        turn = by_question.get(case.get("question"))
        if turn is None:
            skipped.append({"case_id": case.get("id"), "reason": "没有这一轮的产物"})
            continue
        if not case_sources(case):
            # 寒暄类没有金标片段：它们考的是"不该去检索"，不是检索覆盖
            skipped.append({"case_id": case.get("id"), "reason": "这一题没有金标片段（寒暄类）"})
            continue
        if not any(segment_starts(sources, task_id)
                   for task_id, _ in case_sources(case)):
            skipped.append({"case_id": case.get("id"),
                            "reason": "产物里没有片段→时间映射（旧版抓取只存了全文）"})
            continue
        row = evaluate_turn(turn, case, sources)
        if row is None:
            skipped.append({"case_id": case.get("id"),
                            "reason": "这一轮没有记下检索到的全部切片（只有被引用的）"})
            continue
        rows.append(row)
    if not rows:
        return {
            "sample": 0,
            "reading": ("无样本：检索侧指标需要产物里带 `retrieved`（本轮检索到的全部切片，"
                        "含没被引用的）与 `source_texts[].segments`（片段→时间）。"
                        "旧版抓取只存了被引用的切片与整段全文，**不能拿引用集冒充检索集**"
                        "（那会把召回算高）——所以这里报无样本，不报 0。"),
            "skipped": skipped,
        }
    k_values = sorted({row["k"] for row in rows})
    return {
        "sample": len(rows),
        "k_distribution": k_values,
        "recall_at_k": sum(row["recall"] for row in rows) / len(rows),
        "mrr": sum(row["mrr"] for row in rows) / len(rows),
        "ndcg_at_k": (sum(row["ndcg"] for row in rows if row["ndcg"] is not None)
                      / max(1, sum(1 for row in rows if row["ndcg"] is not None))),
        "per_case": rows,
        "skipped": skipped,
        "reading": ("recall_at_k = 金标片段被本次检索（全部 k 条）覆盖的比例，**k 是模型自选的"
                    "条数**，报数必须连 k 一起报；mrr = 第一条相关切片的排名倒数；"
                    "ndcg_at_k = 二值增益的归一化折损累计增益。三者都由片段时间窗与金标片段号"
                    "算得，**零模型调用**——判定模型换族/换思考开关都不会动它们。"),
        "not_measured": ("切块粒度与金标片段是多对一关系（一个窗口盖住多句），因此这里是"
                         "「覆盖」口径而不是「精确命中」；不能与判定式的 context recall 相减。"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    """单独对一份产物算检索指标（零调用）。

    存在的理由：标定实验一次产出多份 `chat_answers_<arm>.json`，它们不能都覆盖
    标准产物 `chat_answers.json`——那样 L0 会分不清"当前标准跑"与"某一臂"。
    这里按显式路径读，产物与读数一起留档。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--qa-set", type=Path, default=DEFAULT_QA_SET)
    parser.add_argument("--output", type=Path, default=None,
                        help="把完整读数（含逐 case）写成 JSON；不传则只打印摘要")
    args = parser.parse_args(list(argv) if argv is not None else None)

    artifact = load_artifact(args.artifact)
    if artifact is None:
        print(f"读不到产物：{args.artifact}", file=sys.stderr)
        return 2
    try:
        qa_set = json.loads(args.qa_set.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"读不到标注集：{args.qa_set}（{error}）", file=sys.stderr)
        return 2
    summary = summarize(artifact, qa_set)
    compact = {key: value for key, value in summary.items()
               if key not in ("per_case", "skipped", "reading", "not_measured")}
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DEFAULT_ARTIFACT", "DEFAULT_QA_SET", "load_artifact", "segment_starts",
           "evaluate_turn", "summarize", "main"]
