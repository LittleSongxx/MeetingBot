import json, statistics
from pathlib import Path
EVAL=Path(__file__).resolve().parent
runs=[json.load(open(EVAL/f"results/m1b_ragas_run{ i }.json")) for i in (1,2,3)]
# per-case mean
cases={}
for run in runs:
    for c in run["cases"]:
        d=cases.setdefault(c["case_id"],{"source":c.get("source_set") or "unknown","metrics":{}})
        for m,v in c["scores"].items():
            if v is not None: d["metrics"].setdefault(m,[]).append(v)
rows=[]
for cid,d in cases.items():
    rows.append({"cid":cid,"source":d["source"],**{m:statistics.mean(v) for m,v in d["metrics"].items() if v}})
print("n cases",len(rows))
for m in ("faithfulness","answer_relevancy","context_precision","context_recall"):
    vals=[r.get(m) for r in rows if r.get(m) is not None]
    vals.sort()
    print(m, "mean %.4f median %.4f min %.2f p10 %.2f p25 %.2f p75 %.2f >=0.9 %d/%d" % (
        statistics.mean(vals), statistics.median(vals), vals[0], vals[int(0.1*(len(vals)-1))], vals[int(0.25*(len(vals)-1))], vals[int(0.75*(len(vals)-1))], sum(1 for v in vals if v>=0.9), len(vals)))
print("\nby source:")
sources=sorted(set(r["source"] for r in rows))
for s in sources:
    sub=[r for r in rows if r["source"]==s]
    print(s, "n",len(sub), {m: round(statistics.mean([r[m] for r in sub if r.get(m) is not None]),3) for m in ("faithfulness","answer_relevancy","context_precision","context_recall")})
