"""意图识别研究：用产品同族 qwen 对 27 条轨迹题做 5 类意图分类，对照 gold 算混淆矩阵。

不集成产品；只验证“显式 intent router”是否有足够准确率。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from lib import model_client  # noqa: E402

INTENTS = ("single_fact", "aggregation", "cross_meeting", "state", "smalltalk")
SYSTEM = """你是会议问答的意图分类器。只输出 JSON：
{"intent":"single_fact|aggregation|cross_meeting|state|smalltalk","reason":"一句话"}
定义：
- single_fact：问一个事实/时间/人物/结论，通常一次检索即可；
- aggregation：问清单/全部/有哪些/哪几项，需要汇总结构化事实；
- cross_meeting：要求比较多段录音/两场会/上午与下午，或分别说明各自内容；
- state：问已确认决策/待办/负责人/项目状态/个人偏好；
- smalltalk：寒暄、天气、常识，不需要会议资料。"""


def env() -> None:
    for line in (EVAL.parent / "meetingbot/fastapi-app/.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    os.environ.setdefault("QWEN_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
    os.environ.setdefault("QWEN_MODEL", os.environ.get("LLM_MODEL", ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    env()
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    cases = gold.get("cases") or []
    url, model, key = model_client.endpoint("qwen")
    rows, confusion = [], Counter()
    for case in cases:
        prompt = f"【问题】\n{case['question']}\n\n请分类并输出 JSON。"
        try:
            parsed = model_client.call_json(url, model, key, SYSTEM, prompt,
                                            timeout=120, total_deadline=240, thinking=False)
            parsed = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else parsed
            predicted = parsed.get("intent") if isinstance(parsed, dict) else None
            reason = parsed.get("reason") if isinstance(parsed, dict) else ""
        except Exception as error:  # noqa: BLE001
            predicted, reason = None, str(error)[:200]
        actual = case.get("intent")
        rows.append({"case_id": case["case_id"], "question": case["question"],
                     "gold": actual, "predicted": predicted, "reason": reason})
        confusion[(actual, predicted)] += 1
        print(f"[{len(rows)}] {case['case_id']} gold={actual} pred={predicted}", flush=True)
    correct = sum(1 for row in rows if row["gold"] == row["predicted"])
    result = {"model": model, "n": len(rows), "accuracy": correct / len(rows) if rows else None,
              "confusion": {f"{gold_intent}->{pred}": count
                            for (gold_intent, pred), count in confusion.items()},
              "rows": rows}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("model", "n", "accuracy", "confusion")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
