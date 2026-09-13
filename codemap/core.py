"""Versioned graph checks and pure task/batch transformations.

These checks establish format and reference consistency, not semantic truth.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .diagnostics import ProtocolError, review_issues


CATALOG = json.loads(Path(__file__).with_name("node_kinds.json").read_text(encoding="utf-8"))
TABLES = ("sources", "entities", "memberships", "contexts", "ports", "flows", "relations", "evidence", "tasks")
DATA_RELATIONS = {"data", "read", "write"}
CODE_RELATIONS = {"call", "control", "inherit", "implements", "reference", "event"}


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def _records(graph, table):
    rows = graph.get(table)
    require(isinstance(rows, list), f"{table}: expected an array")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and nonempty(row.get("id")), f"{table}: missing record id")
        require(row["id"] not in result, f"{table}: duplicate id {row['id']}")
        result[row["id"]] = row
    return result


def _refs(row, key, target, label):
    values = row.get(key)
    require(isinstance(values, list), f"{label}.{key}: expected an array")
    require(all(isinstance(v, str) and v in target for v in values), f"{label}.{key}: unknown reference")
    require(len(values) == len(set(values)), f"{label}.{key}: duplicate reference")
    return values


def _acyclic(parents, label):
    # Iterative traversal also handles nesting deeper than Python's recursion limit.
    finished = set()
    for start in parents:
        stack = [(start, False)]
        active = set()
        while stack:
            current, leaving = stack.pop()
            if leaving:
                active.discard(current)
                finished.add(current)
            elif current not in finished:
                require(current not in active, f"{label}: cycle involving {current}")
                active.add(current)
                stack.append((current, True))
                stack.extend((p, False) for p in parents.get(current, []))


def validate_graph(graph, source_root=None):
    require(isinstance(graph, dict), "graph: expected an object")
    require(graph.get("format_version") == "0.1", "unsupported graph format")
    project = graph.get("project", {})
    require(isinstance(project, dict), "project: expected an object")
    require(all(nonempty(project.get(k)) for k in ("id", "name", "snapshot_id")), "project identity is required")
    require(project.get("mode") == "whole_repository", "v0.1 requires a whole-repository project")
    require(integer(project.get("revision")), "project.revision must be a non-negative integer")
    require(type(project.get("inventory_complete")) is bool, "inventory_complete must be boolean")
    require(project.get("execution") in {"active", "paused"}, "invalid execution state")
    tables = {name: _records(graph, name) for name in TABLES}
    review_issues(graph).raise_if_any('findings', pending=('graph references and remaining field checks', 'source disk checks when requested'))
    sources, entities, evidence = tables["sources"], tables["entities"], tables["evidence"]
    contexts, ports, flows = tables["contexts"], tables["ports"], tables["flows"]
    paths = set()
    for s in sources.values():
        path = s.get("path")
        require(nonempty(path) and "\\" not in path, f"{s['id']}: use a repository-relative POSIX path")
        p = PurePosixPath(path)
        require(not p.is_absolute() and ".." not in p.parts and ":" not in path and str(p) == path and path != ".", f"{s['id']}: invalid source path")
        require(path not in paths, f"duplicate source path: {path}")
        paths.add(path)
        require(s.get("category") in {"source", "build", "documentation", "resource"}, f"{s['id']}: invalid category")
        require(type(s.get("included")) is bool, f"{s['id']}: included must be boolean")
        require(s.get("read_state") in {"unread", "read", "failed"}, f"{s['id']}: invalid read state")
        require(type(s.get("symbols_complete")) is bool, f"{s['id']}: symbols_complete must be boolean")
        require(nonempty(s.get("sha256")) and len(s["sha256"]) == 64 and all(c in "0123456789abcdef" for c in s["sha256"]), f"{s['id']}: invalid SHA-256")
        if not s["included"] or s["read_state"] == "failed":
            require(nonempty(s.get("reason")), f"{s['id']}: exclusion/failure reason required")
    actual_files = {}
    if source_root is not None:
        root = Path(source_root).resolve()
        for s in sources.values():
            target = (root / s["path"]).resolve()
            require(target.is_relative_to(root), f"{s['id']}: source resolves outside repository")
            require(target.is_file(), f"{s['id']}: source is unavailable")
            content = target.read_bytes()
            require(sha256(content).hexdigest() == s["sha256"], f"{s['id']}: source snapshot changed")
            actual_files[s["id"]] = content
    for e in evidence.values():
        require(e.get("source_id") in sources, f"{e['id']}: unknown evidence source")
        require(e.get('freshness', 'current') in {'current', 'stale'}, f"{e['id']}: invalid evidence freshness")
        require(nonempty(e.get('sha256')) and len(e['sha256']) == 64 and all(c in '0123456789abcdef' for c in e['sha256']), f"{e['id']}: invalid evidence hash")
        require(e.get('freshness') == 'stale' or e.get("sha256") == sources[e["source_id"]]["sha256"], f"{e['id']}: evidence does not match snapshot")
        require(integer(e.get("start_line"), 1) and integer(e.get("end_line"), e["start_line"]), f"{e['id']}: invalid evidence range")
        require(nonempty(e.get("note")), f"{e['id']}: evidence note required")
        if actual_files and e.get('freshness') != 'stale':
            raw = actual_files[e['source_id']]
            try:
                text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
            except UnicodeError:
                require(False, f"{e['id']}: evidence source has unsupported text encoding")
            require(e["end_line"] <= max(1, len(text.splitlines())), f"{e['id']}: evidence range exceeds file")
    for e in entities.values():
        require(e.get("kind") in CATALOG["kinds"], f"{e['id']}: unknown node kind")
        require(nonempty(e.get("name")), f"{e['id']}: name required")
        require(isinstance(e.get("language"), str), f"{e['id']}: language required")
        _refs(e, "roles", CATALOG["roles"], e["id"])
        _refs(e, "source_ids", sources, e["id"])
        proofs = _refs(e, "evidence_ids", evidence, e["id"])
        require(e.get("analysis") in {"located", "partial", "reviewed"}, f"{e['id']}: invalid analysis state")
        require(e.get("freshness") in {"current", "stale"}, f"{e['id']}: invalid freshness")
        require(isinstance(e.get("summary"), str), f"{e['id']}: summary required")
        if 'structure' in e:
            anchor = e['structure']
            require(isinstance(anchor, dict) and anchor.get('source_id') in e['source_ids'], f"{e['id']}: invalid structural source")
            require(isinstance(anchor.get('sha256'), str) and len(anchor['sha256']) == 64 and set(anchor['sha256']) <= set('0123456789abcdef'), f"{e['id']}: invalid structural hash")
            require(integer(anchor.get('start_line'), 1) and integer(anchor.get('end_line'), anchor['start_line']), f"{e['id']}: invalid structural range")
            require(isinstance(anchor.get('signature'), str) and isinstance(anchor.get('parameters'), list) and
                    all(isinstance(p, dict) and isinstance(p.get('name'), str) and isinstance(p.get('type'), str) for p in anchor['parameters']),
                    f"{e['id']}: invalid structural signature/parameters")
        if e["analysis"] == "reviewed":
            empty_inventory = e['kind'] in {'repository', 'directory'} and not sources and project['inventory_complete'] and graph.get('inventory', {}).get('policy') == 'whole-tree-v1' and not graph['inventory'].get('gaps')
            require(nonempty(e["summary"]) and (proofs or empty_inventory), f"{e['id']}: reviewed entity requires summary and evidence")
            if e["kind"] in {"function", "method"}:
                details = e.get("details", {})
                require(isinstance(details, dict), f"{e['id']}: function details must be an object")
                require(all(isinstance(details.get(k), list) for k in ("inputs", "outputs", "calls", "reads", "writes", "conditions")), f"{e['id']}: function-level record is incomplete")
    for m in tables["memberships"].values():
        require(m.get("parent_id") in entities and m.get("child_id") in entities, f"{m['id']}: unknown membership entity")
        require(m.get("axis") in {"physical", "semantic", "architecture"}, f"{m['id']}: invalid grouping axis")
    for axis in ("physical", "semantic", "architecture"):
        parents = {}
        for m in tables["memberships"].values():
            if m["axis"] == axis:
                parents.setdefault(m["child_id"], []).append(m["parent_id"])
        _acyclic(parents, f"{axis} memberships")
    for c in contexts.values():
        if c.get("parent_id") is None:
            require(c.get("caller_id") is None and c.get("callee_id") is None, f"{c['id']}: root context has no call target")
        else:
            require(c["parent_id"] in contexts, f"{c['id']}: unknown parent context")
            require(c.get("caller_id") in entities and c.get("callee_id") in entities, f"{c['id']}: unknown caller/callee")
            require(c.get("callsite_evidence_id") in evidence, f"{c['id']}: callsite evidence required")
    _acyclic({c["id"]: [c["parent_id"]] if c.get("parent_id") else [] for c in contexts.values()}, "contexts")
    for p in ports.values():
        require(p.get("entity_id") in entities and p.get("context_id") in contexts, f"{p['id']}: invalid port owner/context")
        require(p.get("direction") in {"in", "out"}, f"{p['id']}: invalid port direction")
        require(p.get("channel") in {"data", "call"}, f"{p['id']}: invalid port channel")
        require(nonempty(p.get("name")) and isinstance(p.get("type"), str), f"{p['id']}: port name/type required")
    for f in flows.values():
        require(f.get("producer_port_id") in ports, f"{f['id']}: unknown flow producer")
        producer = ports[f["producer_port_id"]]
        require(producer["direction"] == "out" and producer["channel"] == "data", f"{f['id']}: producer must be a data output")
        require(nonempty(f.get("name")) and nonempty(f.get("color_key")), f"{f['id']}: name/color identity required")
        require(nonempty(f.get("value_version")), f"{f['id']}: value version required")
        _refs(f, "derived_from", flows, f["id"])
        _refs(f, "evidence_ids", evidence, f["id"])
    require(len({f["color_key"] for f in flows.values()}) == len(flows), "each flow needs a distinct color identity")
    _acyclic({f["id"]: f["derived_from"] for f in flows.values()}, "flow derivation")
    for r in tables["relations"].values():
        require(r.get("kind") in DATA_RELATIONS | CODE_RELATIONS, f"{r['id']}: invalid relation kind")
        require(r.get("basis") in {"source", "candidate", "unresolved"}, f"{r['id']}: invalid evidence basis")
        require(r.get("freshness") in {"current", "stale"}, f"{r['id']}: invalid relation freshness")
        proofs = _refs(r, "evidence_ids", evidence, r["id"])
        require(proofs if r["basis"] == "source" else nonempty(r.get("reason")), f"{r['id']}: evidence or uncertainty reason required")
        require(r.get("context_id") in contexts, f"{r['id']}: context required")
        context = contexts[r["context_id"]]
        if r["kind"] in DATA_RELATIONS:
            require(r.get("from_id") in ports and r.get("to_id") in ports, f"{r['id']}: unknown data port")
            a, b = ports[r["from_id"]], ports[r["to_id"]]
            require(a["direction"] == "out" and b["direction"] == "in" and a["channel"] == b["channel"] == "data", f"{r['id']}: data edge must connect output to input")
            require(r.get("flow_id") in flows, f"{r['id']}: flow identity required")
            if a["context_id"] == b["context_id"]:
                require(a["context_id"] == context["id"], f"{r['id']}: mismatched local call context")
            else:
                require({a["context_id"], b["context_id"]} == {context["id"], context.get("parent_id")}, f"{r['id']}: data crosses unrelated call contexts")
                child_port = a if a["context_id"] == context["id"] else b
                require(child_port["entity_id"] == context["callee_id"], f"{r['id']}: data is attached to the wrong callee")
        else:
            require(r.get("from_id") in entities and r.get("to_id") in entities, f"{r['id']}: unknown relation entity")
            require("flow_id" not in r, f"{r['id']}: code relation must not pretend to be a data flow")
            if r["kind"] == "call":
                require(context.get("caller_id") == r["from_id"] and context.get("callee_id") == r["to_id"], f"{r['id']}: callsite target mismatch")
    for name in ('entities', 'contexts', 'ports', 'flows', 'relations'):
        for row in tables[name].values():
            require(row.get('freshness', 'current') in {'current', 'stale'}, f"{row['id']}: invalid freshness")
            proofs = list(row.get('evidence_ids', []))
            if row.get('callsite_evidence_id'):
                proofs.append(row['callsite_evidence_id'])
            if row.get('freshness', 'current') == 'current':
                require(all(evidence[p].get('freshness', 'current') == 'current' for p in proofs), f"{row['id']}: current record cannot use stale evidence")
    tasks = tables["tasks"]
    for t in tasks.values():
        require(t.get("kind") in {"inventory", "analyze", "link", "update"}, f"{t['id']}: invalid task kind")
        require(t.get("state") in {"queued", "running", "blocked", "done"}, f"{t['id']}: invalid task state")
        require(type(t.get("required")) is bool and integer(t.get("attempt")), f"{t['id']}: invalid task metadata")
        _refs(t, "scope_ids", entities, t["id"])
        _refs(t, "source_ids", sources, t["id"])
        _refs(t, "depends_on", tasks, t["id"])
        if t["state"] == "running":
            lease = t.get("lease")
            require(isinstance(lease, dict), f"{t['id']}: running task needs a lease")
            require(nonempty(lease.get("id")) and nonempty(lease.get("worker")), f"{t['id']}: invalid lease identity")
            require(type(lease.get("expires_at")) in {int, float} and math.isfinite(lease["expires_at"]), f"{t['id']}: invalid lease expiry")
        else:
            require(t.get("lease") is None, f"{t['id']}: inactive task cannot retain a lease")
        if t["state"] == "blocked":
            require(nonempty(t.get("reason")), f"{t['id']}: blocking reason required")
    _acyclic({t["id"]: t["depends_on"] for t in tasks.values()}, "task dependencies")
    receipts = graph.get("receipts")
    require(isinstance(receipts, dict), "receipts must be an object")
    for receipt in receipts.values():
        require(isinstance(receipt, dict) and nonempty(receipt.get("digest")) and integer(receipt.get("revision"), 1) and receipt["revision"] <= project["revision"], "invalid batch receipt")
    canonical(graph)
    return graph


def completion_report(graph):
    validate_graph(graph)
    included = [s for s in graph["sources"] if s["included"]]
    code = [s for s in included if s["category"] == "source"]
    included_ids = {s['id'] for s in included}
    entities = [e for e in graph["entities"] if e["kind"] != "external" and (not e['source_ids'] or included_ids.intersection(e['source_ids']))]
    outstanding = [t["id"] for t in graph["tasks"] if t["required"] and t["state"] != "done"]
    incomplete_entities = [e["id"] for e in entities if e["analysis"] != "reviewed" or e["freshness"] != "current"]
    uncertain = [r["id"] for r in graph["relations"] if r["basis"] != "source" or r["freshness"] != "current"]
    stale_records = [r['id'] for name in ('contexts', 'ports', 'flows') for r in graph[name] if r.get('freshness') == 'stale']
    read_complete = all(s["read_state"] == "read" for s in included)
    symbols_complete = all(s["symbols_complete"] for s in code)
    if not graph["project"]["inventory_complete"] or graph.get('inventory', {}).get('gaps'):
        status = "inventory_incomplete"
    elif outstanding or not read_complete or not symbols_complete or incomplete_entities:
        status = "partial"
    elif uncertain or stale_records:
        status = "coverage_with_unresolved_relations"
    else:
        status = "complete"
    return {"status": status, "sources_read": sum(s["read_state"] == "read" for s in code), "sources_total": len(code), "outstanding_tasks": outstanding, "incomplete_entities": incomplete_entities, "uncertain_relations": uncertain, "stale_records": stale_records, "exclusions": [s["id"] for s in graph["sources"] if not s["included"]]}


def _revision(graph, expected):
    require(integer(expected) and graph["project"]["revision"] == expected, "stale project revision")


def claim_task(graph, task_id, worker, *, expected_revision, now, lease_seconds=300):
    validate_graph(graph)
    _revision(graph, expected_revision)
    require(graph["project"]["execution"] == "active", "project is paused")
    require(nonempty(worker) and type(now) in {int, float} and math.isfinite(now), "invalid worker/clock")
    require(type(lease_seconds) in {int, float} and 0 < lease_seconds <= 3600, "invalid lease duration")
    updated = deepcopy(graph)
    tasks = {t["id"]: t for t in updated["tasks"]}
    require(task_id in tasks, "unknown task")
    task = tasks[task_id]
    require(task["state"] == "queued", "task is not queued")
    require(all(tasks[d]["state"] == "done" for d in task["depends_on"]), "task dependencies are unfinished")
    task.update(state="running", attempt=task["attempt"] + 1, reason="", lease={"id": str(uuid4()), "worker": worker, "expires_at": now + lease_seconds})
    updated["project"]["revision"] += 1
    return validate_graph(updated)


def renew_task(graph, task_id, lease_id, *, expected_revision, now, lease_seconds=300):
    validate_graph(graph)
    _revision(graph, expected_revision)
    require(type(now) in {int, float} and math.isfinite(now), 'invalid clock')
    require(type(lease_seconds) in {int, float} and math.isfinite(lease_seconds) and 0 < lease_seconds <= 3600, 'invalid lease duration')
    updated = deepcopy(graph)
    task = next((t for t in updated['tasks'] if t['id'] == task_id), None)
    require(task is not None and task['state'] == 'running' and task['lease']['id'] == lease_id, 'stale execution lease')
    require(task['lease']['expires_at'] > now, 'execution lease expired')
    task['lease']['expires_at'] = max(task['lease']['expires_at'], now + lease_seconds)
    updated['project']['revision'] += 1
    return validate_graph(updated)


def retry_task(graph, task_id, *, expected_revision):
    validate_graph(graph)
    _revision(graph, expected_revision)
    updated = deepcopy(graph)
    task = next((t for t in updated['tasks'] if t['id'] == task_id), None)
    require(task is not None and task['state'] == 'blocked', 'only a blocked task can be retried')
    require(task['kind'] != 'inventory', 'inventory gaps require a fresh scan')
    task.update(state='queued', reason='Retry requested; the previous blocking condition still needs verification.')
    updated['project']['revision'] += 1
    return validate_graph(updated)


def recover_expired(graph, *, expected_revision, now):
    validate_graph(graph)
    _revision(graph, expected_revision)
    require(type(now) in {int, float} and math.isfinite(now), "invalid clock")
    updated = deepcopy(graph)
    changed = False
    for task in updated["tasks"]:
        if task["state"] == "running" and task["lease"]["expires_at"] <= now:
            task.update(state="queued", lease=None, reason="Previous execution lease expired; resume from the last committed batch.")
            changed = True
    if changed:
        updated["project"]["revision"] += 1
    return validate_graph(updated)


def apply_batch(graph, batch, *, now):
    validate_graph(graph)
    require(isinstance(batch, dict) and batch.get("format_version") == "0.1", "unsupported batch format")
    require(set(batch) - {'deletes'} == {"format_version", "project_id", "snapshot_id", "base_revision", "batch_id", "task_id", "lease_id", "upserts", "new_tasks", "result", "reason"}, "invalid batch fields")
    require(nonempty(batch.get("batch_id")), "batch identity required")
    require(batch.get("project_id") == graph["project"]["id"] and batch.get("snapshot_id") == graph["project"]["snapshot_id"], "batch project/snapshot mismatch")
    fingerprint = digest(batch)
    previous = graph["receipts"].get(batch["batch_id"])
    if previous:
        require(previous["digest"] == fingerprint, "batch id reused with different content")
        return deepcopy(graph)
    _revision(graph, batch.get("base_revision"))
    require(type(now) in {int, float} and math.isfinite(now), "invalid clock")
    updated = deepcopy(graph)
    tasks = {t["id"]: t for t in updated["tasks"]}
    require(batch.get("task_id") in tasks, "unknown batch task")
    task = tasks[batch["task_id"]]
    require(task["state"] == "running" and task["lease"]["id"] == batch.get("lease_id"), "stale execution lease")
    require(task["lease"]["expires_at"] > now, "execution lease expired")
    deletes = batch.get('deletes', {})
    require(isinstance(deletes, dict) and set(deletes) <= {'entities', 'memberships', 'contexts', 'ports', 'flows', 'relations', 'evidence'}, 'unsupported retirement table')
    if any(deletes.values()):
        from .updates import retired_closure, remove_records
        require(task['kind'] in {'update', 'link'} and nonempty(batch.get('reason')), 'retirement requires an update task and a review explanation')
        for name, ids in deletes.items():
            require(isinstance(ids, list) and all(isinstance(id, str) for id in ids) and len(ids) == len(set(ids)), 'retirement IDs must be unique strings')
            current = {r['id']: r for r in graph[name]}
            require(set(ids) <= set(task.get('review_ids', {}).get(name, [])) & current.keys(), 'retirement is outside this update task')
            if name == 'entities':
                require(all(current[id]['kind'] not in {'file', 'directory', 'repository'} for id in ids), 'inventory entities require a snapshot update')
        removed = retired_closure(updated, deletes)
        require(all(not (set(r['id'] for r in batch.get('upserts', {}).get(name, [])) & ids) for name, ids in removed.items()), 'cannot retire and upsert the same record')
        remove_records(updated, removed)
    upserts = batch.get("upserts")
    require(isinstance(upserts, dict) and set(upserts) <= set(TABLES) - {"tasks"}, "unsupported batch table")
    for name, records in upserts.items():
        incoming = _records({name: records}, name)
        current = {r["id"]: r for r in updated[name]}
        for key, row in incoming.items():
            if name == "sources" and key in current:
                require(all(row.get(k) == current[key][k] for k in ("path", "sha256", "included", "category")), "batch cannot change source snapshot or narrow scope")
            if name == "sources":
                require(key in current, "new sources require a new inventory snapshot")
            if name == "entities" and key in current:
                require(row.get("kind") == current[key]["kind"] or current[key]["kind"] == "unknown", "entity kind change requires an explicit identity migration")
            current[key] = deepcopy(row)
        updated[name] = list(current.values())
    for incoming in _records({"new_tasks": batch.get("new_tasks")}, "new_tasks").values():
        require(incoming["id"] not in tasks, "task already exists")
        require(incoming.get("state") == "queued" and incoming.get("attempt") == 0 and incoming.get("lease") is None, "new tasks must start queued")
        require(incoming.get("required") is True, "discovered repository work must remain required")
        updated["tasks"].append(deepcopy(incoming))
    result = batch.get("result")
    require(result in {"partial", "done", "blocked"}, "invalid batch result")
    require(isinstance(batch.get("reason"), str), "batch reason must be text")
    review_issues(updated, task=task, result=result).raise_if_any('findings_and_completion',
        pending=('graph references and remaining field checks', 'source disk checks'))
    if result != "done":
        require(nonempty(batch["reason"]), "unfinished work requires a continuation/blocking note")
    elif task["kind"] in {"analyze", "update"}:
        final_entities = {e["id"]: e for e in updated["entities"]}
        final_sources = {s["id"]: s for s in updated["sources"]}
        require(all(final_entities[e]["analysis"] == "reviewed" and final_entities[e]["freshness"] == "current" for e in task["scope_ids"]), "task scope has not reached its analysis depth")
        require(all(final_sources[s]["read_state"] == "read" and (final_sources[s]["category"] != "source" or final_sources[s]["symbols_complete"]) for s in task["source_ids"]), "task sources are not fully read/indexed")
    if result == 'done' and task.get('review_ids'):
        for name, ids in task['review_ids'].items():
            rows = {r['id']: r for r in updated[name]}
            require(all(id not in rows or rows[id].get('freshness', 'current') == 'current' for id in ids), 'update task still has stale records to reconcile')
        final_entities = {e['id']: e for e in updated['entities']}
        require(all(final_entities[id]['analysis'] == 'reviewed' and final_entities[id]['freshness'] == 'current'
                    for id in task['scope_ids']), 'update summaries have not been reviewed')
    task.update(state={"partial": "queued", "done": "done", "blocked": "blocked"}[result], lease=None, reason=batch["reason"])
    updated["project"]["revision"] += 1
    updated["receipts"][batch["batch_id"]] = {"digest": fingerprint, "revision": updated["project"]["revision"]}
    return validate_graph(updated)
