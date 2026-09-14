"""Fixed-seed C2 edge accuracy sample for the checked-out mypy repository."""
from __future__ import annotations
import ast, json, random, re, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap import skeleton
from codemap.project import open_project

SEED = 20260914

def source_text(root, path):
    return (root / path).read_text(encoding="utf-8", errors="ignore")

def fn_body_features(text, start, end):
    lines=text.splitlines()
    body="\n".join(lines[max(0,start-1):end])
    dynamic=bool(re.search(r"\b(getattr|setattr|hasattr|__getattribute__|callback|decorator|overload|lambda)\b", body))
    return body, dynamic

def symbol_rows(store):
    with skeleton.connection(store) as db:
        rows=[]
        for r in db.execute("SELECT n.*, f.path FROM nodes n JOIN files f ON f.id=n.file_id WHERE n.kind IN ('function','method') AND f.path LIKE '%.py' AND n.start_line>0"):
            rows.append(dict(r))
        return rows

def ast_call_refs(root, name, rows):
    by_path={}
    for row in rows: by_path.setdefault(row["path"], []).append(row)
    refs=[]
    for path,items in by_path.items():
        try: tree=ast.parse(source_text(root,path), filename=path)
        except SyntaxError: continue
        parents=[]
        def visit(node, owner=None):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                owner=next((r for r in items if r["start_line"]==node.lineno), owner)
            if isinstance(node,ast.Call):
                called = node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else None
                if called==name and owner:
                    refs.append({"path":path,"line":node.lineno,"caller_id":owner["id"],"caller_name":owner["name"]})
            for child in ast.iter_child_nodes(node): visit(child,owner)
        visit(tree)
    return refs

def edge_nodes(store, result):
    return {r["id"]: r for r in result["symbols"]}

def main():
    root=Path(sys.argv[1]).resolve(); project=Path(sys.argv[2]).resolve(); output=Path(sys.argv[3]).resolve()
    store=open_project(project)
    rows=symbol_rows(store)
    rng=random.Random(SEED)
    ordinary=[]; dynamic=[]; cross=[]
    for row in rows:
        text=source_text(root,row["path"])
        body,is_dynamic=fn_body_features(text,row["start_line"],row["end_line"])
        if is_dynamic and row["name"] not in {"__init__", "sum", "isinstance"}: dynamic.append(row)
        elif row["name"] not in {"__init__", "accept", "isinstance", "sum", "len", "type", "str"} and re.search(r"\w+\s*\(", body): ordinary.append(row)
    with skeleton.connection(store) as db:
        cross=[dict(r) for r in db.execute("SELECT n.*,f.path FROM nodes n JOIN files f ON f.id=n.file_id WHERE n.kind IN ('function','method') AND (f.path LIKE '%.py' OR f.path LIKE '%.pyi') AND n.name NOT LIKE '__%' ORDER BY n.degree DESC,n.id")]
    rng.shuffle(ordinary); rng.shuffle(dynamic)
    def distinct(items):
        result=[]; names=set(); paths=set()
        for item in items:
            if item["name"] in names or item["path"] in paths: continue
            result.append(item); names.add(item["name"]); paths.add(item["path"])
            if len(result)==10: break
        return result
    cross_chosen=cross[:10]
    if len(cross_chosen)<10:
        cross_chosen.extend([row for row in rows if row not in cross_chosen][:10-len(cross_chosen)])
    chosen=distinct(ordinary)+distinct(dynamic)+cross_chosen
    groups=["ordinary"]*10+["dynamic"]*10+["cross_module_high_indegree"]*10
    records=[]
    all_files=list({p for p in [x["path"] for x in rows]})
    for group,row in zip(groups,chosen):
        text=source_text(root,row["path"]); body,dynamic_flag=fn_body_features(text,row["start_line"],row["end_line"])
        callees=skeleton.calls(store,row["id"],direction="callees",depth=1,limit=500)
        callers=skeleton.calls(store,row["id"],direction="callers",depth=1,limit=500)
        callee_symbols=edge_nodes(store,callees); caller_symbols=edge_nodes(store,callers)
        direct_names=set(re.findall(r"\b([A-Za-z_]\w*)\s*\(",body))
        callee_checks=[]
        for edge in callees["edges"]:
            dst=callee_symbols.get(edge["dst_id"],{})
            name=dst.get("name","")
            callee_checks.append({"edge":edge,"callee":name,"source_name_match":name in direct_names or name in body,
                                  "classification":"dynamic-boundary" if dynamic_flag and name not in direct_names else "direct-or-lexical"})
        grep=ast_call_refs(root,row["name"],rows)
        caller_locations=[{"id":x.get("id"),"name":x.get("name"),"path":x.get("path"),"start_line":x.get("start_line")} for x in caller_symbols.values()]
        records.append({"group":group,"symbol_id":row["id"],"name":row["name"],"qualified_name":row["qualified_name"],
                        "path":row["path"],"start_line":row["start_line"],"end_line":row["end_line"],
                        "source_excerpt":body[:4000],"library_callees":callee_checks,"library_callers":caller_locations,
                        "grep_reference_set":grep,"callee_precision_review":"source-and-AST self-review",
                        "caller_recall_review":"AST call-site reference set; same-name non-calls excluded"})
    output.write_text(json.dumps({"seed":SEED,"groups":{"ordinary":10,"dynamic":10,"cross_module_high_indegree":10},"records":records},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"seed":SEED,"records":len(records),"output":str(output)},ensure_ascii=False))

if __name__=="__main__": main()
