# Scale-path implementation record

Scope: TASK_SCALE_BENCH.md stage A. Stage B requires separate user confirmation and a new independent task using the requested model; no mypy benchmark has been run here.

## A1 — targeted FTS maintenance

- Added an indexed node-to-FTS-rowid map, including a one-time mapping migration for existing indexes.
- Semantic submit updates only its batch; sync updates added/deleted/changed/invalidated nodes. No-op sync does not repair or rewrite FTS.
- Full rebuild is explicit: `python -m codemap reindex MAP` or `blueprint_reindex(project=...)`.
- Regression covers search after submit, stable unaffected row identities, no implicit full refresh, and explicit corruption repair.
- Full Python suite: 168 tests, 167 passed and one existing Windows symlink-permission skip. JavaScript: 48 passed.

A2–A4 pending. Ten-thousand-line acceptance remains the last completed scale benchmark; no claim of 100K validation.
