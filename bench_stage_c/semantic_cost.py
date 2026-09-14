"""Claim and submit a 500-symbol semantic-cost sample with real token counts."""
from __future__ import annotations
import json, statistics, sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4
import tiktoken
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap import skeleton
from codemap.executor import claim, submit
from codemap.project import open_project

def main():
    root=Path(sys.argv[1]).resolve(); project=Path(sys.argv[2]).resolve(); output=Path(sys.argv[3]).resolve(); target_stratum=sys.argv[4] if len(sys.argv)>4 else 'natural'; target_count=int(sys.argv[5]) if len(sys.argv)>5 else 500; target_kind=sys.argv[6] if len(sys.argv)>6 else 'function_method'
    store=open_project(project); enc=tiktoken.get_encoding('cl100k_base')
    skeleton.sync(store,budget=100000000)
    with skeleton.connection(store) as db:
        kind_sql="kind IN ('function','method')" if target_kind=='function_method' else "kind='class'"
        all_nodes=[(r[0],r[1]) for r in db.execute(f"SELECT id,degree FROM nodes WHERE {kind_sql} ORDER BY degree DESC,id")]
        degrees=[r[1] for r in all_nodes]; top_cut=degrees[max(0,int(len(degrees)*0.10)-1)]; mid_low=min(degrees[int(len(degrees)*0.45)],degrees[int(len(degrees)*0.55)]); mid_high=max(degrees[int(len(degrees)*0.45)],degrees[int(len(degrees)*0.55)])
        n=len(all_nodes); top_ids={x[0] for x in all_nodes[:max(1,int(n*.10))]}; middle_ids={x[0] for x in all_nodes[int(n*.45):int(n*.55)]}; tail_ids={x[0] for x in all_nodes[-max(1,int(n*.10)):]} 
        if target_stratum != 'natural':
            ids=[]
            for row in db.execute(f"SELECT t.node_id,n.degree FROM semantic_tasks t JOIN nodes n ON n.id=t.node_id WHERE t.state='queued' AND n.{kind_sql}"):
                label='top10pct' if row[0] in top_ids else 'middle10pct' if row[0] in middle_ids else 'long_tail' if row[0] in tail_ids else 'other'
                if label==target_stratum: ids.append(row[0])
            db.execute("UPDATE semantic_tasks SET priority=-1000000 WHERE state='queued'")
            db.executemany("UPDATE semantic_tasks SET priority=1000000000 WHERE node_id=? AND state='queued'",[(i,) for i in ids])
    rows=[]; batches=[]; batch_no=0
    while len(rows)<target_count:
        claim_started=perf_counter(); claimed=claim(store,'stage-c-semantic-cost',n=min(20,target_count-len(rows))); claim_seconds=perf_counter()-claim_started
        tasks=claimed['tasks']
        if not tasks: raise RuntimeError(f'claim stopped at {len(rows)}: {claimed["state"]}')
        results=[]; batch_rows=[]; batch_no+=1; submit_started=perf_counter()
        for task in tasks:
            context=task['source']+'\nCALL_CONTEXT\n'+json.dumps(task['calls'],sort_keys=True,ensure_ascii=False)
            input_tokens=len(enc.encode(context))
            summary=f"The symbol {task['signature']} is summarized from its provided source and call context."
            detail={'model':'stub','source_bytes':len(task['source'].encode('utf-8')),'call_context_items':len(task['calls'])}
            output_tokens=len(enc.encode(summary+'\n'+json.dumps(detail,sort_keys=True)))
            result={'node_id':task['node_id'],'body_sha':task['body_sha'],'lease_id':task['lease_id'],'summary':summary,'detail':detail,
                    'evidence':[{'node_id':task['node_id'],'body_sha':task['body_sha'],'start_line':task['start_line'],'end_line':task['end_line']}],
                    'model':'stage-c-local-stub-cl100k_base','used_tokens':output_tokens}
            with skeleton.connection(store) as db:
                degree=db.execute('SELECT degree FROM nodes WHERE id=?',(task['node_id'],)).fetchone()[0]
            with skeleton.connection(store) as db:
                rank=db.execute(f"SELECT row_number FROM (SELECT id, row_number() OVER (ORDER BY degree DESC,id) AS row_number FROM nodes WHERE {kind_sql}) WHERE id=?",(task['node_id'],)).fetchone()[0]
                total=db.execute(f"SELECT count(*) FROM nodes WHERE {kind_sql}").fetchone()[0]
            stratum='top10pct' if rank<=max(1,int(total*.10)) else 'middle10pct' if int(total*.45)<rank<=int(total*.55) else 'long_tail' if rank>total-max(1,int(total*.10)) else 'other'
            batch_rows.append({'node_id':task['node_id'],'path':task['path'],'signature':task['signature'],'degree':degree,'stratum':stratum,
                               'input_tokens':input_tokens,'output_tokens':output_tokens,'estimated_tokens':task['estimated_tokens']})
            results.append(result)
        receipt=submit(store,{'batch_id':f'stage-c-{uuid4()}','results':results})
        submit_seconds=perf_counter()-submit_started
        for row in batch_rows: row['submit_seconds_batch']=submit_seconds; rows.append(row)
        batches.append({'batch':batch_no,'count':len(batch_rows),'claim_seconds':claim_seconds,'submit_seconds':submit_seconds,'accepted':receipt['accepted']})
    by={}
    for row in rows: by.setdefault(row['stratum'],[]).append(row)
    report={'tokenizer':'tiktoken cl100k_base','sample_count':len(rows),'kind':target_kind,'seed':'scheduler-order-after-fixed-map','sampling_mode':target_stratum,'priority_preconditioned':target_stratum!='natural','degree_cutoffs':{'top10pct':top_cut,'middle10pct':[mid_low,mid_high]},
            'batches':batches,'symbols':rows,'strata_summary':{k:{'count':len(v),'input_tokens':sum(x['input_tokens'] for x in v),'output_tokens':sum(x['output_tokens'] for x in v),'median_input_tokens':statistics.median(x['input_tokens'] for x in v),'median_output_tokens':statistics.median(x['output_tokens'] for x in v),'submit_seconds_total':sum(x['submit_seconds_batch'] for x in v)} for k,v in by.items()}}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'sample_count':len(rows),'strata':{k:len(v) for k,v in by.items()},'output':str(output)}))

if __name__=='__main__': main()
