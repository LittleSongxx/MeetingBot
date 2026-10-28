import json, statistics
from pathlib import Path
EVAL=Path(__file__).resolve().parent
def load_scores(path):
    d=json.load(open(path)); return {c["case_id"]:c["scores"] for c in d["cases"]}, d
arms={
 "M1b": ("results/m1b_ragas_run1.json","results/m1b_ragas_run2.json","results/m1b_ragas_run3.json"),
 "P1b": ("results/m1d_ragas_run1.json",),
 "P1c": ("results/m1e_ragas_run1.json",),
}
ma={}
for arm,files in arms.items():
    merged={}
    for f in files:
        sc,_=load_scores(f)
        for cid,d in sc.items():
            b=merged.setdefault(cid,{})
            for m,v in d.items():
                if v is not None: b.setdefault(m,[]).append(v)
    ma[arm]={cid:{m:statistics.mean(v) for m,v in d.items()} for cid,d in merged.items()}
print("arm, metric, overall, cross30, non-cross30")
for arm in ma:
    rows=ma[arm]
    cs={cid:v for cid,v in rows.items() if cid in ("m001","m002","m003","m004","m005","m006","m007","m008","m009","m010","m011","m012","m013","m014","m015","m016","m017","m018","m019","m020","m021","m022","m023","m024","m025","m026","m027","m028","m029","m030")}
    for metric in ("faithfulness","answer_relevancy","context_precision","context_recall"):
        allv=[v[metric] for v in rows.values() if v.get(metric) is not None]
        cross=[v[metric] for v in cs.values() if v.get(metric) is not None]
        other=[v[metric] for cid,v in rows.items() if cid not in cs and v.get(metric) is not None]
        print(arm,metric,"%.3f"%statistics.mean(allv),"%.3f"%statistics.mean(cross),"%.3f"%statistics.mean(other))
