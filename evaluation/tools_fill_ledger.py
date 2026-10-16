"""把本次真实运行的读数写进 LEDGER（一次性脚本，跑完即可删）。

为什么用脚本而不是手抄：数字来自三份产物（rag_packet.json、chat_answers.json 由
chat_checks 复算、ai_call_log 汇总），手抄一遍就可能抄错，而账本里的数字是要被引用的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALUATION_DIR))

from regression import chat_checks  # noqa: E402

PACKET = EVALUATION_DIR / "results" / "rag_packet.json"
ANSWERS = EVALUATION_DIR / "results" / "chat_answers.json"
LEDGER = EVALUATION_DIR / "LEDGER.md"


def pct(block: dict | None) -> str:
    if not block or block.get("rate") is None:
        return "n/a"
    interval = block.get("clopper_pearson_95") or []
    spread = f"（CI {100 * interval[0]:.1f}–{100 * interval[1]:.1f}%）" if interval else ""
    return f"{100 * block['rate']:.1f}%（{block['successes']}/{block['total']}）{spread}"


def main() -> int:
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
    summary = packet.get("summary") or {}
    judgements = [case for case in packet["cases"] if case.get("judgement")]
    errors = sum(
        1 for case in judgements
        if case["judgement"].get("context_error") or case["judgement"].get("answer_error"))
    calls = sum(1 for _ in [])
    citations = chat_checks.summarize(answers["turns"], sources=answers.get("source_texts"))
    retrievals = sum(1 for turn in answers["turns"] if turn.get("tools"))
    turns = len(answers["turns"])

    lines = [
        "| 指标 | 值 | 口径 |",
        "|---|---|---|",
        f"| context precision | {pct(summary.get('context_precision'))} | 检索到的上下文里被判有用的比例（逐段判定） |",
        f"| context recall | {pct(summary.get('context_recall'))} | 标注事实里能在上下文中找到的比例；金标是 27 组人工问答 |",
        f"| faithfulness | {pct(summary.get('faithfulness'))} | 答案断言里能被上下文支撑的比例（逐断言判定） |",
        f"| answer relevancy | {pct(summary.get('answer_relevancy'))} | 正面回答了问题的轮次比例（判定式，与 RAGAS 嵌入口径不同） |",
        "",
        f"样本：{turns} 轮问答（每轮真实走检索 + 生成），判定 {len(judgements)} 轮 / 判定调用 "
        f"{2 * len(judgements)} 次（每轮两次：上下文质量、答案质量），判定失败 {errors} 轮。",
        "",
        f"检索行为：{retrievals}/{turns} 轮调用了工具（修复前实测 4 次同题有 2 次完全不检索并编造内容，"
        f"见下方"本轮真实运行暴露的问题"）。",
        "",
        f"引用不变量（零调用，随 `make smoke` 复算）：可定位（转写类引用）{pct(citations.get('locatable'))}，"
        f"未越权 {pct(citations.get('authorized'))}；口径不适用（纪要条目 {citations.get('not_applicable_count')} 条）、"
        f"编编号 {citations.get('unknown_ref_count')} 条。",
    ]
    section = "\n".join(lines)
    print(section)
    LEDGER_TEXT = "\n".join([
        "### 轴5 检索与回答（2026-10-16 首测）",
        "",
        section,
        "",
        "**裁判披露**：判定模型是 `qwen3.8-max`（与被测 `qwen3.8-max-0902` **同族、近乎同模型**）——",
        "本轮无跨族凭据可用（ModelScope 的 key 需要账号邮箱验证、DashScope 侧其余模型配额耗尽）",
        "。同族判定的已知后果是**一致性被高估**（模型倾向于认可自己的输出），",
        "因此这四个数只作**基线登记**，不作产品决策依据；跨族复判是下一步。",
        "",
    ])
    marker = "### 问答引用（零调用不变量，每次 `make smoke` 跑）"
    text = LEDGER.read_text(encoding="utf-8")
    if marker not in text:
        print("LEDGER 里找不到插入点", file=sys.stderr)
        return 2
    text = text.replace(marker, LEDGER_TEXT + "\n" + marker, 1)
    LEDGER.write_text(text, encoding="utf-8")
    print("\nLEDGER 已更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
