"""Reproducible Stage-B benchmark for a checked-out repository.

The script keeps each map under --output, records raw per-run timings, and
never mutates the checked-out source tree.  It is intentionally dependency-
light so the bundled Python runtime can run it on Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codemap.project import initialize_project, open_project
from codemap import skeleton


def tracked_files(root: Path) -> list[Path]:
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    return [root / p for p in raw.decode().split("\0") if p]


def inventory(root: Path) -> dict:
    rows = []
    for path in tracked_files(root):
        data = path.read_bytes()
        lines = data.count(b"\n") + bool(data and not data.endswith(b"\n"))
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": len(data), "lines": lines})
    return {"files": len(rows), "lines": sum(r["lines"] for r in rows),
            "bytes": sum(r["bytes"] for r in rows), "rows": rows}


def db_sizes(map_dir: Path) -> dict:
    db = map_dir / "map.sqlite"
    wal = map_dir / "map.sqlite-wal"
    return {"map_sqlite_bytes": db.stat().st_size if db.exists() else 0,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0}


def timed(fn):
    started = perf_counter()
    value = fn()
    return round(perf_counter() - started, 6), value


def run_init(root: Path, output: Path, repeat: int) -> dict:
    map_dir = output / f"init-{repeat}"
    elapsed, store = timed(lambda: initialize_project(root, map_dir))
    with skeleton.connection(store) as db:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("files", "nodes", "edges")}
    size = db_sizes(map_dir)
    return {"run": repeat, "seconds": elapsed, **size, **counts, "store": str(map_dir)}


def query_benchmark(store, root: Path) -> dict:
    with skeleton.connection(store) as db:
        names = [dict(r) for r in db.execute(
            "SELECT id,name FROM nodes WHERE kind IN ('function','method') ORDER BY id LIMIT 20")]
    samples = {key: [] for key in ("find_symbol_exact", "find_symbol_fuzzy", "callers", "callees", "search")}
    for row in names:
        actions = (
            ("find_symbol_exact", lambda: skeleton.find_symbol(store, row["id"])),
            ("find_symbol_fuzzy", lambda: skeleton.find_symbol(store, row["name"], fuzzy=True)),
            ("callers", lambda: skeleton.calls(store, row["id"], depth=2)),
            ("callees", lambda: skeleton.calls(store, row["id"], direction="callees", depth=2)),
            ("search", lambda: skeleton.search(store, row["name"])),
        )
        for key, action in actions:
            elapsed, _ = timed(action)
            samples[key].append(round(elapsed * 1000, 3))
    paths = ["", "mypy", "mypyc", "test-data/unit"]
    repo_map = []
    for path in paths:
        elapsed, _ = timed(lambda p=path: skeleton.repo_map(store, p))
        repo_map.append({"path": path, "seconds": elapsed})
    return {"samples": samples, "median_ms": {k: median(v) for k, v in samples.items()},
            "repo_map": repo_map, "source": str(root)}


def mutation_benchmark(store, root: Path, output: Path) -> dict:
    candidates = [p for p in tracked_files(root) if p.suffix == ".py" and p.stat().st_size < 200_000]
    one = next(p for p in candidates if any(
        line.startswith((" ", "\t")) and line.lstrip().startswith("return ")
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()))
    twenty = candidates[:20]
    records = {"one_function": [], "twenty_files": [], "cosmetic": []}
    originals = {p: p.read_bytes() for p in set([one, *twenty])}
    try:
        for repeat in range(3):
            text = one.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            idx = next(i for i, line in enumerate(lines) if line.lstrip().startswith("return ") and line.startswith((" ", "\t")))
            lines[idx] = lines[idx].rstrip("\r\n") + "  # benchmark body mutation\n"
            one.write_text("".join(lines), encoding="utf-8", newline="")
            elapsed, result = timed(lambda: skeleton.sync(store))
            records["one_function"].append({"run": repeat + 1, "seconds": elapsed,
                                             "parsed_files": result["parsed_files"], "suspect": len(result["suspect"])})
            one.write_bytes(originals[one])
            skeleton.sync(store)

            for p in twenty:
                p.write_bytes(originals[p] + b"\n# benchmark multi-file change\n")
            elapsed, result = timed(lambda: skeleton.sync(store))
            records["twenty_files"].append({"run": repeat + 1, "seconds": elapsed,
                                             "parsed_files": result["parsed_files"], "suspect": len(result["suspect"])})
            for p in twenty:
                p.write_bytes(originals[p])
            skeleton.sync(store)

            one.write_bytes(originals[one] + b"\n# benchmark cosmetic comment\n")
            elapsed, result = timed(lambda: skeleton.sync(store))
            records["cosmetic"].append({"run": repeat + 1, "seconds": elapsed,
                                         "parsed_files": result["parsed_files"], "suspect": len(result["suspect"])})
            one.write_bytes(originals[one])
            skeleton.sync(store)
    finally:
        for path, data in originals.items():
            path.write_bytes(data)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inv = inventory(root)
    runs = [run_init(root, output, i + 1) for i in range(args.repeats)]
    store = open_project(runs[-1]["store"])
    query_runs = [query_benchmark(store, root) for _ in range(3)]
    mutation_runs = mutation_benchmark(store, root, output)
    report = {"repository": str(root), "head": subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        "inventory": {k: inv[k] for k in ("files", "lines", "bytes")},
        "init_runs": runs, "query_runs": query_runs, "mutation_runs": mutation_runs,
        "python_lines": sum(r["lines"] for r in inv["rows"] if Path(r["path"]).suffix in (".py", ".pyi")),
    }
    (output / "BENCH_100K.raw.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
