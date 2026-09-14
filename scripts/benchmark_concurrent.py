"""Reproducible sync + two submitters + query concurrency benchmark."""
from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap import skeleton
from codemap.executor import claim, submit
from codemap.project import open_project


def stamp():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=1000000)
    args = parser.parse_args()
    store = open_project(args.map)
    with skeleton.connection(store) as db:
        root = Path(skeleton._meta(db, "root"))
        query_node = db.execute("SELECT id FROM nodes WHERE kind IN ('function','method') ORDER BY degree DESC,id LIMIT 1").fetchone()[0]
    claimed = claim(store, "concurrent-submit-prep", n=2)
    if len(claimed["tasks"]) != 2:
        raise RuntimeError(f"need two leased tasks, got {claimed['state']}")
    errors = []
    events = []
    event_lock = threading.Lock()
    done = threading.Event()
    target = next(root / p for p in ("mypy/main.py", "mypy/build.py") if (root / p).exists())
    original = target.read_bytes()

    def record(kind, started, ended, started_clock, ended_clock, **extra):
        with event_lock:
            events.append({"kind": kind, "started_at": started, "ended_at": ended,
                           "elapsed_ms": round((ended_clock - started_clock) * 1000, 3), **extra})

    def submit_one(task):
        started_clock = perf_counter(); started = stamp()
        try:
            result = {"node_id": task["node_id"], "body_sha": task["body_sha"], "lease_id": task["lease_id"],
                      "summary": "concurrency benchmark", "detail": {"benchmark": True},
                      "evidence": [{"node_id": task["node_id"], "body_sha": task["body_sha"],
                                    "start_line": task["start_line"], "end_line": task["end_line"]}],
                      "model": "stage-b-concurrency", "used_tokens": 0}
            receipt = submit(store, {"batch_id": f"concurrent-{uuid4()}", "results": [result]})
            ended_clock = perf_counter(); record("submit", started, stamp(), started_clock, ended_clock, accepted=receipt["accepted"], node_id=task["node_id"])
        except Exception as exc:
            ended_clock = perf_counter(); record("submit", started, stamp(), started_clock, ended_clock, error=repr(exc)); errors.append(repr(exc))

    def sync_one():
        started_clock = perf_counter(); started = stamp()
        try:
            target.write_bytes(original + b"\n# concurrent sync probe\n")
            result = skeleton.sync(store, budget=args.budget)
            target.write_bytes(original)
            skeleton.sync(store, budget=args.budget)
            ended_clock = perf_counter(); record("sync", started, stamp(), started_clock, ended_clock, parsed_files=result["parsed_files"], suspect=len(result["suspect"]))
        except Exception as exc:
            target.write_bytes(original); ended_clock = perf_counter(); record("sync", started, stamp(), started_clock, ended_clock, error=repr(exc)); errors.append(repr(exc))

    def query_loop():
        while not done.is_set():
            started = perf_counter(); started_at = stamp()
            try:
                skeleton.find_symbol(store, query_node)
                elapsed = (perf_counter() - started) * 1000
                with event_lock:
                    events.append({"kind": "query", "started_at": started_at, "ended_at": stamp(), "elapsed_ms": round(elapsed, 3)})
            except Exception as exc:
                errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=4) as pool:
        query_future = pool.submit(query_loop)
        futures = [pool.submit(submit_one, task) for task in claimed["tasks"]]
        futures.append(pool.submit(sync_one))
        for future in futures:
            future.result()
        done.set(); query_future.result()
    queries = [event["elapsed_ms"] for event in events if event["kind"] == "query"]
    queries.sort()
    p95 = queries[max(0, int(len(queries) * 0.95) - 1)] if queries else None
    report = {"map": str(args.map), "events": sorted(events, key=lambda x: x["started_at"]),
              "errors": errors, "query_count": len(queries), "query_p95_ms": p95,
              "database_is_locked": any("database is locked" in error for error in errors)}
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
