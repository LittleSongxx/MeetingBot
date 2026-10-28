import json, statistics, re, csv
from pathlib import Path
EVAL=Path(__file__).resolve().parent
packet=json.loads((EVAL/"results"/"m1b_packet.json").read_text(encoding="utf-8"))
refs=json.loads((EVAL/"gold"/"qa_ref_answers.json").read_text(encoding="utf-8"))
qa=json.loads((EVAL/"results"/"qa_eval100_fresh.json").read_text(encoding="utf-8"))
runs=[json.loads((EVAL/f"results/m1b_ragas_run{i}.json").read_text(encoding="utf-8")) for i in (1,2,3)]
rm={c["case_id"]:c for c in refs["cases"]}
pm={c["case_id"]:c for c in packet["cases"]}
qm={c["id"]:c for c in qa["cases"]}
scores={}
for run in runs:
    for c in run["cases"]:
        d=scores.setdefault(c["case_id"],{})
        for m,v in c["scores"].items():
            if v is not None: d.setdefault(m,[]).append(v)
def mmean(d,m):
    vals=d.get(m) or []
    return statistics.mean(vals) if vals else None
def overlap(answer, reference):
    a=set(re.findall(r"[\u4e00-\u9fff0-9]", answer or "")); r=set(re.findall(r"[\u4e00-\u9fff0-9]", reference or ""))
    return len(a&r)/len(r) if r else 0
rows=[]
for cid,case in pm.items():
    if case.get("_source")=="x": pass
    q=qm.get(cid,{})
    if q.get("source_set")!="cross30": continue
    ans=case.get("answer") or ""; ref=rm[cid]["reference"]
    sc=scores.get(cid,{})
    row={"case_id":cid,
         "faith":mmean(sc,"faithfulness"),"ar":mmean(sc,"answer_relevancy"),
         "cp":mmean(sc,"context_precision"),"cr":mmean(sc,"context_recall"),
         "n_ctx":len(case.get("contexts") or []),
         "ans_len":len(ans),"facts":len(case.get("expected_facts") or []),
         "overlap":round(overlap(ans,ref),2),
         "insufficient": int(bool(re.search(r"资料不足|未找到|没有找到|未确认|尚未|草稿|仅有", ans))),
         "question":case["question"],"answer":ans[:600].replace("\n"," / "),"reference":ref[:400]}
    rows.append(row)
rows.sort(key=lambda r:((r["cp"] if r["cp"] is not None else 0), (r["cr"] if r["cr"] is not None else 0)))
out=EVAL.parent/"analysis"/"optimization"/"cross30_diagnosis.csv"
out.parent.mkdir(parents=True,exist_ok=True)
with out.open("w",encoding="utf-8",newline="") as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print("wrote",out,"n",len(rows))
print("mean scores", {m:round(statistics.mean([r[m] for r in rows if r[m] is not None]),3) for m in ("faith","ar","cp","cr")})
print("insufficient-answer cases", sum(r["insufficient"] for r in rows))
print("low overlap (<0.55)", sum(1 for r in rows if r["overlap"]<0.55))
for r in rows[:10]:
    print(r["case_id"], "cp=%.2f cr=%.2f faith=%.2f ar=%.2f"%(r["cp"] or 0,r["cr"] or 0,r["faith"] or 0,r["ar"] or 0), "ctx",r["n_ctx"],"ov",r["overlap"],"ins",r["insufficient"])
