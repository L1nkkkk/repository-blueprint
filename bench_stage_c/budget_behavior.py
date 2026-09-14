"""Measure claim priority under 10% and 30% semantic budgets."""
from __future__ import annotations
import json, sys
from pathlib import Path
from time import perf_counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from codemap import skeleton
from codemap.executor import claim
from codemap.project import open_project

def main():
    project=Path(sys.argv[1]).resolve(); output=Path(sys.argv[2]).resolve(); fraction=float(sys.argv[3]); store=open_project(project)
    with skeleton.connection(store) as db:
        full=sum(r[0] for r in db.execute("SELECT estimated_tokens FROM semantic_tasks"))
        degrees=[r[0] for r in db.execute("SELECT degree FROM nodes WHERE kind IN ('function','method')")]; degrees.sort(reverse=True); top_cut=degrees[max(0,int(len(degrees)*.10)-1)]
    budget=int(full*fraction); skeleton.sync(store,budget=budget); claimed=[]; batches=[]
    while True:
        started=perf_counter(); c=claim(store,'stage-c-budget-'+str(fraction),n=20); elapsed=perf_counter()-started
        if not c['tasks']: break
        rows=[]
        for t in c['tasks']:
            with skeleton.connection(store) as db: degree=db.execute('SELECT degree FROM nodes WHERE id=?',(t['node_id'],)).fetchone()[0]
            rows.append({'node_id':t['node_id'],'degree':degree,'top10':degree>=top_cut,'estimated_tokens':t['estimated_tokens']}); claimed.append(rows[-1])
        batches.append({'count':len(rows),'claim_seconds':elapsed,'remaining':c['budget_remaining'],'rows':rows})
    report={'fraction':fraction,'full_estimated_tokens':full,'budget':budget,'top10_cutoff':top_cut,'claimed_count':len(claimed),'top10_claimed':sum(r['top10'] for r in claimed),'top10_coverage':sum(r['top10'] for r in claimed)/len(claimed) if claimed else 0,'batches':batches}
    output.write_text(json.dumps(report,indent=2),encoding='utf-8'); print(json.dumps({k:report[k] for k in ('fraction','full_estimated_tokens','budget','claimed_count','top10_claimed','top10_coverage')}))
if __name__=='__main__': main()
