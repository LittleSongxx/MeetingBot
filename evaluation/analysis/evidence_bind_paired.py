"""S1 机制级配对回放：在同一份答案/上下文上应用绑定，再重判。

目的：把"绑定机制本身"的效应与"两次运行之间的波动"分开。跨臂 A/B 会混入
生成随机性（同配置两次运行 faithfulness 极差可达 5pp）；配对回放只改答案里的引用。

用法：
    evaluation/.venv/bin/python evaluation/analysis/evidence_bind_paired.py
输出：results/cross30_bindoff_replay_packet.json（待判 packet）
"""
import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent
sys.path.insert(0, str(REPO / "meetingbot/fastapi-app"))
from services.citation_bind import bind_answer  # noqa: E402

SRC = EVAL / "results/cross30_bindoff_packet.json"
OUT = EVAL / "results/cross30_bindoff_replay_packet.json"

packet = json.loads(SRC.read_text(encoding="utf-8"))
rebound = 0
changed = 0
cases = []
for case in packet["cases"]:
    bound = bind_answer(case.get("answer") or "", case.get("contexts") or [])
    rebound += bound.rebound
    if bound.changed:
        changed += 1
    mutated = dict(case)
    mutated["answer"] = bound.answer
    mutated.pop("judgement", None)
    cases.append(mutated)
out = {key: value for key, value in packet.items() if key not in {"summary", "judgements"}}
out["cases"] = cases
out["purpose"] = "S1 配对回放：同一答案/上下文，只应用 citation binding（评测用，非产品输出）"
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"source": str(SRC), "out": str(OUT), "cases": len(cases),
                  "rebound": rebound, "changed_cases": changed}, ensure_ascii=False))
