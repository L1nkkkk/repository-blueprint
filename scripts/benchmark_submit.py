"""Measure ten hash-bound semantic claim/submit batches on an existing map."""
import argparse, json, sys
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap.project import open_project
from codemap.executor import claim, submit

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("map", type=Path); ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args(); store = open_project(a.map); rows=[]
    for i in range(10):
        t0=perf_counter(); c=claim(store, "stage-b-benchmark", n=1); claim_s=perf_counter()-t0
        if not c["tasks"]: rows.append({"batch":i+1,"claim_seconds":claim_s,"state":c["state"]}); break
        task=c["tasks"][0]
        result={"node_id":task["node_id"],"body_sha":task["body_sha"],"lease_id":task["lease_id"],
                "summary":"benchmark submission", "detail":{"benchmark":True},
                "evidence":[{"node_id":task["node_id"],"body_sha":task["body_sha"],"start_line":task["start_line"],"end_line":task["end_line"]}],
                "model":"stage-b-benchmark", "used_tokens":0}
        t1=perf_counter(); accepted=submit(store,{"batch_id":"bench-"+str(uuid4()),"results":[result]}); submit_s=perf_counter()-t1
        rows.append({"batch":i+1,"claim_seconds":claim_s,"submit_seconds":submit_s,"accepted":accepted["accepted"]})
    claims=[r["claim_seconds"]*1000 for r in rows if "claim_seconds" in r]; submits=[r["submit_seconds"]*1000 for r in rows if "submit_seconds" in r]
    out={"batches":rows,"claim_median_ms":median(claims) if claims else None,"submit_median_ms":median(submits) if submits else None}
    a.output.write_text(json.dumps(out,indent=2),encoding="utf-8"); print(json.dumps(out,indent=2))
if __name__ == "__main__": main()
