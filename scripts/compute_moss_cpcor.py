"""MOSS 条件 8 场 cpCER 汇总（与既有报告同口径、同工具）。"""
import json, sys
sys.path.insert(0, "/home/song/code/Agent/media_agent/AIMeeting/evaluation")
from asr.cpcor import compute
from pathlib import Path

BASE = Path("/home/song/code/Agent/media_agent/AIMeeting/evaluation/asr/first_test")
meetings = ["R8001_M8004", "R8003_M8001", "R8007_M8010", "R8007_M8011",
            "R8008_M8013", "R8009_M8018", "R8009_M8019", "R8009_M8020"]
results = []
for m in meetings:
    task = BASE / f"task_{m}.moss.json"
    if not task.exists():
        print(f"skip {m}: snapshot missing"); continue
    r = compute(task, Path("/home/song/code/Agent/media_agent/AIMeeting/evaluation/.venv/bin/python"),
                BASE / "cpcor")
    r["condition"] = m + ".moss"
    results.append(r)
if results:
    macro = {k: round(sum(r[k] for r in results)/len(results), 4)
             for k in ("cpcor", "cer_no_punct", "delta_cp")}
    out = {"condition": "moss", "meetings": results, "macro": macro}
    (BASE / "cpcor" / "cpcor_report_moss_8m.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("MOSS 8-meeting macro:", macro)
    for r in results:
        print(f"  {r['session']}: cpCER {r['cpcor']:.3f} CER {r['cer_no_punct']:.3f} "
              f"Δcp {r['delta_cp']:+.3f} hyp_spk {r['hyp_speakers']}")
