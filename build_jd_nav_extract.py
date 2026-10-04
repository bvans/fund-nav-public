#!/usr/bin/env python3
import gzip,json,re
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parent
targets=json.loads((ROOT/"analysis_targets.json").read_text(encoding="utf-8"))["names"]
def norm(s):
    return re.sub(r"\s+","",str(s or "")).replace("（","(").replace("）",")").lower()
latest=json.loads((ROOT/"data/all/latest_nav.json").read_text(encoding="utf-8"))
rows=latest if isinstance(latest,list) else latest.get("funds",latest.get("data",[]))
by_norm={norm(x.get("name")):x for x in rows}
matched={}
missing=[]
for q in targets:
    x=by_norm.get(norm(q))
    if not x:
        cand=[y for y in rows if norm(q) in norm(y.get("name")) or norm(y.get("name")) in norm(q)]
        x=cand[0] if len(cand)==1 else None
    if x: matched[q]=x
    else: missing.append(q)
codes={str(x["code"]).zfill(6) for x in matched.values()}
hist={c:[] for c in codes}
for p in sorted((ROOT/"data/all/history/2026").glob("*.csv.gz")):
    try: df=pd.read_csv(p,dtype={"code":str},compression="gzip")
    except Exception: continue
    df["code"]=df["code"].astype(str).str.zfill(6)
    for _,r in df[df["code"].isin(codes)].iterrows():
        hist[r["code"]].append({"nav_date":str(r["nav_date"]),"unit_nav":float(r["unit_nav"]),"accum_nav":None if pd.isna(r.get("accum_nav")) else float(r.get("accum_nav"))})
out={"missing":missing,"funds":[]}
for q,x in matched.items():
    c=str(x["code"]).zfill(6)
    out["funds"].append({"query_name":q,"code":c,"name":x.get("name"),"latest_nav_date":x.get("nav_date"),"latest_unit_nav":x.get("unit_nav"),"history":hist.get(c,[])})
dest=ROOT/"data/analysis/jd_2026_nav.json"; dest.parent.mkdir(parents=True,exist_ok=True)
dest.write_text(json.dumps(out,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
print(f"matched={len(matched)} missing={len(missing)}")
print("missing:",missing)
