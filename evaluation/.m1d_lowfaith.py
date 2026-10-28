import json
p=json.load(open("results/m1d_packet.json")); c={x["case_id"]:x for x in p["cases"]}
run=json.load(open("results/m1d_ragas_run1.json")); r={x["case_id"]:x for x in run["cases"]}
rows=[]
for cid,x in c.items():
    s=r.get(cid,{}).get("scores",{})
    f=s.get("faithfulness")
    if f is not None: rows.append((f,cid,x["question"],x["answer"],len(x["contexts"]),s.get("context_recall")))
rows.sort()
for f,cid,q,a,nctx,cr in rows[:12]:
    print("="*80); print(cid,"faith=%.2f cr=%.2f ctx=%d"%(f,cr or -1,nctx)); print("Q:",q); print("A:", (a or "")[:700].replace("\n"," / "))
PY
