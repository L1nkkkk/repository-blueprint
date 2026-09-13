"""Generate a hand-curated protocol fixture; this is not a code analyzer."""

from hashlib import sha256
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from codemap.core import validate_graph


def make_graph():
    paths = ["src/motion.cpp", "src/diagnostics.cpp", "tests/test_motion.cpp", "CMakeLists.txt", "tools/report.cpp"]
    sources = []
    for index, path in enumerate(paths):
        sources.append({"id": f"src:{index}", "path": path, "category": "build" if index == 3 else "source", "sha256": sha256((BASE / "sample_repo" / path).read_bytes()).hexdigest(), "included": True, "read_state": "read" if index in {0, 3} else "unread", "symbols_complete": index in {0, 3}, "reason": ""})

    def entity(id, kind, name, src, *, reviewed=False, roles=None):
        result = {"id": id, "kind": kind, "name": name, "language": "cpp" if src else "", "roles": roles or [], "source_ids": src, "analysis": "reviewed" if reviewed else "located", "freshness": "current", "summary": f"示例：{name}", "evidence_ids": ["ev:motion"] if reviewed else []}
        if kind in {"function", "method"} and reviewed:
            result["details"] = {"inputs": ["position", "velocity", "dt"] if id == "fn:integrate" else ["velocity", "dt", "state"], "outputs": ["next position"] if id == "fn:integrate" else [], "calls": ["IntegratePosition"] if id == "fn:move" else [], "reads": ["state.position"] if id == "fn:move" else [], "writes": ["state.position"] if id == "fn:move" else [], "conditions": []}
        return result

    entities = [
        entity("repo:sample", "repository", "MotionSample", [], reviewed=True),
        entity("dir:src", "directory", "src", [], reviewed=True),
        entity("file:motion", "file", "motion.cpp", ["src:0"], reviewed=True),
        entity("scope:motion", "namespace", "motion", ["src:0"], reviewed=True),
        entity("type:movement", "class", "MovementSystem", ["src:0"], reviewed=True),
        entity("fn:move", "method", "Move", ["src:0"], reviewed=True),
        entity("fn:integrate", "function", "IntegratePosition", ["src:0"], reviewed=True),
        entity("field:position", "field", "State.position", ["src:0"], reviewed=True),
        entity("fn:measure", "function", "Measure", ["src:1"]),
        entity("fn:test", "function", "main", ["src:2"], roles=["entry", "test"]),
        entity("fn:display", "function", "DisplayUnits", ["src:4"], roles=["tool"]),
    ]
    evidence = [
        {"id": "ev:motion", "source_id": "src:0", "sha256": sources[0]["sha256"], "start_line": 1, "end_line": 15, "note": "人工整理的示例类型、方法和位置计算"},
        {"id": "ev:call", "source_id": "src:0", "sha256": sources[0]["sha256"], "start_line": 11, "end_line": 12, "note": "Move 调用积分函数并写回状态"},
    ]
    contexts = [
        {"id": "ctx:move", "parent_id": None, "caller_id": None, "callee_id": None},
        {"id": "ctx:integrate", "parent_id": "ctx:move", "caller_id": "fn:move", "callee_id": "fn:integrate", "callsite_evidence_id": "ev:call"},
    ]

    def port(id, owner, name, direction, context):
        return {"id": id, "entity_id": owner, "name": name, "direction": direction, "channel": "data", "type": "float", "context_id": context}

    ports = [
        port("p:velocity-out", "fn:move", "velocity", "out", "ctx:move"),
        port("p:velocity-in", "fn:integrate", "velocity", "in", "ctx:integrate"),
        port("p:next-out", "fn:integrate", "result", "out", "ctx:integrate"),
        port("p:next-in", "fn:move", "next", "in", "ctx:move"),
        port("p:position-out", "field:position", "position before write", "out", "ctx:move"),
        port("p:position-in", "fn:integrate", "position", "in", "ctx:integrate"),
        port("p:dt-out", "fn:move", "dt", "out", "ctx:move"),
        port("p:dt-in", "fn:integrate", "dt", "in", "ctx:integrate"),
    ]
    flows = [
        {"id": "flow:velocity", "name": "velocity", "producer_port_id": "p:velocity-out", "color_key": "D1", "value_version": "argument@move", "derived_from": [], "evidence_ids": ["ev:call"]},
        {"id": "flow:next", "name": "next position", "producer_port_id": "p:next-out", "color_key": "D2", "value_version": "integrate@return", "derived_from": ["flow:velocity", "flow:position", "flow:dt"], "evidence_ids": ["ev:motion"]},
        {"id": "flow:position", "name": "position before write", "producer_port_id": "p:position-out", "color_key": "D3", "value_version": "state.position@before", "derived_from": [], "evidence_ids": ["ev:call"]},
        {"id": "flow:dt", "name": "dt", "producer_port_id": "p:dt-out", "color_key": "D4", "value_version": "argument-dt@move", "derived_from": [], "evidence_ids": ["ev:call"]},
    ]
    relations = [
        {"id": "rel:call", "kind": "call", "from_id": "fn:move", "to_id": "fn:integrate", "context_id": "ctx:integrate", "basis": "source", "freshness": "current", "evidence_ids": ["ev:call"]},
        {"id": "rel:velocity", "kind": "data", "from_id": "p:velocity-out", "to_id": "p:velocity-in", "flow_id": "flow:velocity", "context_id": "ctx:integrate", "basis": "source", "freshness": "current", "evidence_ids": ["ev:call"]},
        {"id": "rel:position", "kind": "data", "from_id": "p:position-out", "to_id": "p:position-in", "flow_id": "flow:position", "context_id": "ctx:integrate", "basis": "source", "freshness": "current", "evidence_ids": ["ev:call"]},
        {"id": "rel:dt", "kind": "data", "from_id": "p:dt-out", "to_id": "p:dt-in", "flow_id": "flow:dt", "context_id": "ctx:integrate", "basis": "source", "freshness": "current", "evidence_ids": ["ev:call"]},
    ]

    def task(id, kind, scope, src, dependencies=None):
        return {"id": id, "kind": kind, "scope_ids": scope, "source_ids": src, "depends_on": dependencies or [], "required": True, "state": "queued", "attempt": 0, "lease": None, "reason": ""}

    graph = {
        "format_version": "0.1",
        "project": {"id": "project:sample", "name": "MotionSample protocol fixture", "snapshot_id": "snapshot:sample-v1", "revision": 0, "mode": "whole_repository", "inventory_complete": True, "execution": "active"},
        "sources": sources, "entities": entities,
        "memberships": [
            {"id": "member:src", "parent_id": "repo:sample", "child_id": "dir:src", "axis": "physical"},
            {"id": "member:file", "parent_id": "dir:src", "child_id": "file:motion", "axis": "physical"},
            {"id": "member:type", "parent_id": "scope:motion", "child_id": "type:movement", "axis": "semantic"},
            {"id": "member:method", "parent_id": "type:movement", "child_id": "fn:move", "axis": "semantic"},
            {"id": "member:function", "parent_id": "scope:motion", "child_id": "fn:integrate", "axis": "semantic"},
        ],
        "contexts": contexts, "ports": ports, "flows": flows, "relations": relations, "evidence": evidence,
        "tasks": [task("task:link", "link", ["fn:move", "fn:integrate"], ["src:0"]), task("task:diagnostics", "analyze", ["fn:measure"], ["src:1"]), task("task:tests", "analyze", ["fn:test"], ["src:2"]), task("task:tools", "analyze", ["fn:display"], ["src:4"])],
        "receipts": {},
    }
    return validate_graph(graph, BASE / "sample_repo")


def make_batch(claimed):
    task = next(t for t in claimed["tasks"] if t["id"] == "task:link")
    return {
        "format_version": "0.1", "project_id": claimed["project"]["id"], "snapshot_id": claimed["project"]["snapshot_id"], "base_revision": claimed["project"]["revision"], "batch_id": "batch:link-result", "task_id": task["id"], "lease_id": task["lease"]["id"],
        "upserts": {"relations": [{"id": "rel:return", "kind": "data", "from_id": "p:next-out", "to_id": "p:next-in", "flow_id": "flow:next", "context_id": "ctx:integrate", "basis": "source", "freshness": "current", "evidence_ids": ["ev:call"]}]},
        "new_tasks": [], "result": "done", "reason": "返回值对应已核对；诊断与测试继续保留在全仓库队列。",
    }


if __name__ == "__main__":
    (BASE / "sample-map.json").write_text(json.dumps(make_graph(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
