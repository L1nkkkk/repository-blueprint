"""Prepare and execute the five C3 suspect propagation scenarios."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap import skeleton
from codemap.project import open_project

SCENARIOS = {
    "single_function_body": {"path":"mypy/gclogger.py","name":"get_stats","kind":"body"},
    "high_indegree_callee_body": {"path":"mypy/subtypes.py","name":"is_subtype","kind":"body"},
    "signature_change": {"path":"mypy/subtypes.py","name":"is_subtype","kind":"signature"},
    "comment_only": {"path":"mypy/subtypes.py","name":"is_subtype","kind":"comment"},
    "function_delete": {"path":"mypy/gclogger.py","name":"get_stats","kind":"delete"},
}

def find(store, spec):
    with skeleton.connection(store) as db:
        row=db.execute("SELECT n.*,f.path FROM nodes n JOIN files f ON f.id=n.file_id WHERE f.path=? AND n.name=? ORDER BY n.start_line LIMIT 1",(spec['path'],spec['name'])).fetchone()
        return dict(row)

def prepare(store, output):
    manifest={"budget":1000000,"scenarios":{}}
    for name,spec in SCENARIOS.items():
        row=find(store,spec); callers=skeleton.calls(store,row['id'],direction='callers',depth=1,limit=500)
        manifest['scenarios'][name]={"spec":spec,"node_id":row['id'],"start_line":row['start_line'],"end_line":row['end_line'],
                                     "expected_changed":[row['id']] if spec['kind'] in ('body','signature') else [],
                                     "expected_suspect":[x['id'] for x in callers['symbols'] if x['id']!=row['id']] if spec['kind']=='signature' else [],
                                     "expected_deleted":[row['id']] if spec['kind']=='delete' else [],
                                     "expected_reason":"contract change propagates to direct callers" if spec['kind']=='signature' else "body/cosmetic/deletion baseline expectation recorded before execution"}
    output.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({"prepared":True,"scenarios":len(manifest['scenarios']),"output":str(output)}))

def mutate(path, spec, start, end):
    original=path.read_text(encoding='utf-8'); lines=original.splitlines(keepends=True)
    if spec['kind']=='body':
        index=max(start, min(end-1,len(lines)-1)); indent=' ' * 4
        lines.insert(index, indent+'pass  # stage C body mutation\n')
    elif spec['kind']=='signature':
        for index in range(start-1,min(end,len(lines))):
            if lines[index].strip()==') -> bool:':
                lines[index]='    _stage_c_marker: object | None = None,\n'+lines[index]
                break
    elif spec['kind']=='comment':
        lines.append('\n# stage C cosmetic comment\n')
    elif spec['kind']=='delete':
        del lines[start-1:end]
    path.write_text(''.join(lines),encoding='utf-8',newline='')
    return original

def run(store, root, manifest_path, output):
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')); results=[]
    for name,item in manifest['scenarios'].items():
        path=root/item['spec']['path']; original=mutate(path,item['spec'],item['start_line'],item['end_line'])
        try:
            result=skeleton.sync(store,budget=manifest['budget'])
            results.append({"scenario":name,"expected":item,"actual":{"changed":result['changed'],"suspect":result['suspect'],"deleted":result['deleted'],"parsed_files":result['parsed_files']},
                            "match":set(result['suspect'])==set(item['expected_suspect'])})
        finally:
            path.write_text(original,encoding='utf-8',newline='')
            skeleton.sync(store,budget=manifest['budget'])
    output.write_text(json.dumps({"manifest":str(manifest_path),"results":results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({"results":len(results),"matches":sum(x['match'] for x in results),"output":str(output)}))

if __name__=='__main__':
    mode=sys.argv[1]; root=Path(sys.argv[2]).resolve(); project=Path(sys.argv[3]).resolve()
    store=open_project(project)
    if mode=='prepare': prepare(store,Path(sys.argv[4]).resolve())
    else: run(store,root,Path(sys.argv[4]).resolve(),Path(sys.argv[5]).resolve())
