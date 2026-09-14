# Scale-path implementation record

Scope: TASK_SCALE_BENCH.md stage A. Stage B requires separate user confirmation and a new independent task using the requested model; no mypy benchmark has been run here.

## A1 — targeted FTS maintenance

- Added an indexed node-to-FTS-rowid map, including a one-time mapping migration for existing indexes.
- Semantic submit updates only its batch; sync updates added/deleted/changed/invalidated nodes. No-op sync does not repair or rewrite FTS.
- Full rebuild is explicit: `python -m codemap reindex MAP` or `blueprint_reindex(project=...)`.
- Regression covers search after submit, stable unaffected row identities, no implicit full refresh, and explicit corruption repair.
- Full Python suite: 168 tests, 167 passed and one existing Windows symlink-permission skip. JavaScript: 48 passed.

A2–A4 pending. Ten-thousand-line acceptance remains the last completed scale benchmark; no claim of 100K validation.

## A2 — affected call-site resolution

- Added persistent file/target/origin indexes for syntax sites. Only changed-file sites and sites matching changed candidate names are resolved again.
- Sites sharing one (source, kind, line) edge key are processed together, preserving other targets on the same line.
- Node name/scope indexes are loaded once; the resolver performs no per-call enclosing-node SELECT.
- Twelve seeded random edit rounds compare subset results to a full reference rebuild, including deleted/reintroduced symbols, duplicate names and same-line calls. A body-only edit resolves exactly its two local sites despite 20 unrelated files.
- Full Python suite: 170 tests, 169 passed and one existing skip. JavaScript: 48 passed.
