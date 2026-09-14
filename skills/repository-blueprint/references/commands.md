# Command and batch guide

## MCP route (preferred when installed)

For the efficient default workflow, read [prepared batches](preparation.md) or `blueprint_guide(topic="preparation")`. MCP init/status/next/commit/task default to bounded summaries; `detail="full"` retains full detail, and query returns complete paginated records. Summary lists carry counts/truncation indicators. Do not treat a summary as a complete upsert or complete task scope. Use context to check saved responsibilities, prepare to fill mechanical fields from actual reads, and commit by prepared_id. The manual complete-batch protocol below remains supported.

Use absolute paths for `root`, `project`, `output`. `blueprint_init(root, output?)` returns `project_directory`; use it as `project` in subsequent calls. It reopens an existing map only when the repository matches. Read `blueprint_status(project, check_snapshot=true)` before continuing.

`blueprint_next(project, worker)` claims a task. `blueprint_read(project, source, start, limit)` and `blueprint_search(project, query, paths, limit)` provide source evidence. `blueprint_query(project, table, ids?, filter?, offset?, limit?, revision?)` reads saved records: follow `next_offset` while preserving `revision`, and restart pagination if the revision changes. Top-level filter values match exact scalars or array membership.

`next` includes the first `reading_pack` page by default. `blueprint_pack(project, task_id|source|entity_id, cursor?, max_chars?, related_limit?)` assembles numbered source, structural outlines, full saved findings and pending work. Follow its cursor with the same selector, and inspect explicit gaps/continuation pointers. Its delivered source lines count for evidence preparation on that MCP connection; metadata alone does not. Use `next(include_pack=false)` only for a metadata-only claim. Details: [module reading packs](reading-pack.md). The CLI provides `pack <map> --source <path>` (or `--task-id` / `--entity-id`); `next --no-pack` opts out.

`blueprint_commit(project, batch)` takes the batch object, not a file path. `blueprint_task(project, action, task_id?, lease_id?, lease_seconds?)` provides renew/retry/recover/pause/resume. `blueprint_export(project, output)` writes a new JSON file. `blueprint_guide(topic, start?, limit?)` exposes the format, task protocol and node catalog.

`blueprint_canvas(project)` returns a URL to open with the host's browser tool. It reuses that connection's server for the same map. Closing the MCP connection stops its canvas servers; starting a new one reopens the same saved map and layout. Merely opening it does not start AI. Explicit canvas Agent requests start an independent local Codex process that survives the canvas connection and uses only the selected map's scoped tools.

## CLI fallback

Run from the tool checkout, or replace `python -m codemap` below with the configured Python followed by the absolute installed plugin `scripts/run.py` path. `ROOT` is the target repository; `MAP` is a new output directory. Quote paths containing spaces. The default output is `ROOT/.codemap`, excluded from its own inventory. Existing projects are never overwritten.

```text
python -m codemap init ROOT --output MAP
python -m codemap status MAP --check-snapshot
python -m codemap next MAP --worker current-reader
python -m codemap read MAP src/example.cpp --start 1 --limit 160
python -m codemap search MAP SymbolName --paths "src/*"
python -m codemap commit MAP batch.json
python -m codemap serve MAP
```

`next` returns one actual lease, task scope, source IDs and a `batch_template`. Use its `project_id`, `snapshot_id`, `base_revision`, `task_id` and `lease_id` exactly. Give every logically new batch a unique ID. For a network/retry uncertainty, resend the identical batch; a changed payload requires a new ID and current revision. By default the reader lease lasts 1,800 seconds, at most 3,600 seconds.

```text
python -m codemap renew MAP TASK_ID LEASE_ID
python -m codemap pause MAP
python -m codemap resume MAP
python -m codemap recover MAP
python -m codemap retry MAP BLOCKED_TASK_ID
python -m codemap export MAP new-result.json
```

Pause stops new claims while allowing an already claimed batch to finish. `recover` only requeues expired leases. `retry` requeues a blocked reading task; it does not fix its cause or bypass dependencies. Resolve inventory gaps and rescan with `blueprint_update`. Renewal changes the graph revision, so update the batch's `base_revision` from the returned project state. Save batches outside the analyzed scope, for example inside `.codemap`, so generated artifacts do not become unexplained source changes.

The batch must contain these fields (an update may additionally include `deletes` as described below):

```json
{
  "format_version": "0.1",
  "project_id": "from-next",
  "snapshot_id": "from-next",
  "base_revision": 1,
  "batch_id": "unique-batch-id",
  "task_id": "from-next",
  "lease_id": "from-next",
  "upserts": {},
  "new_tasks": [],
  "result": "partial",
  "reason": "Actual remaining work"
}
```

Allowed upsert tables are `sources`, `entities`, `memberships`, `contexts`, `ports`, `flows`, `relations`, `evidence`. Use `blueprint_guide(topic="graph-format")`, the installed `graph-format.md` reference, or `GRAPH_FORMAT.md` at the source checkout for their record shapes. Existing source path, fingerprint, category and inclusion cannot change within a snapshot. New sources require a new scan. Unknown entities may resolve to a concrete kind; other kind changes require an identity migration, not an ordinary upsert.

`result` is `partial`, `blocked` or `done`. Incomplete work requires a useful reason. New tasks must be queued and required. A completed file task requires reviewed current scope entities, fully read sources and complete symbol coverage. All new and reused evidence is checked against current disk content at commit. The checker validates structure and source locations; the reader remains responsible for semantic accuracy.

`read` returns at most 500 lines per call with `next_start`, the exact SHA-256 and total lines; it never changes reading progress. Text support is UTF-8 and BOM-marked UTF-16, up to 2 MiB per file. Unreadable source files remain required blocked work. Search with `paths="*"` is a literal, case-insensitive source search (without paths, blueprint_search uses the persistent symbol/signature/summary FTS index) and returns `truncated` plus file failures. Do not treat an empty or limited result as proof of no usages.

An empty text file is represented as one blank logical line with `empty_file=true`; line 1 and its exact fingerprint can support an empty-file review. It does not contain a declaration to invent.

The canvas renders saved results, preserves theme/layout in the map, and refreshes committed graph revisions. Its Agent panel persists questions/analysis requests, actual execution events and answers; node analysis and whole-repository continuation require real validated commits. Pause stops the owned process and releases its leases, resume acquires a new execution identity, and cancel retains committed results. An unavailable reader or an answer without required commits is not completion. The separate coverage panel reports files, scenarios, functions outside scenarios and unresolved/stale relationships.

## Incremental updates in the same map

Call `blueprint_update(project, action="preview")` after source changes. Inspect `changed`, `added`, `deleted`, `renamed`, `change_details`, `affected_paths`, `gaps` and `can_apply`. Apply with `action="apply"`, `expected_revision=base_revision`, the exact `plan_id`, and the same explicit `renames` if supplied. A different source tree or graph revision rejects the plan; preview again. Retry an uncertain successful apply with the same identity. Unreadable inventory gaps prevent migration without altering the stored graph.

The tool advances the snapshot, archives the previous graph in the map database, cancels old-snapshot reading leases, and queues affected files plus a final linking task. It preserves unrelated file progress, source/symbol identities and the separate reading view. `action="history"` lists archived revisions. No AI is started by updating the map or pressing the canvas button.

Use `blueprint_next` to obtain a new lease. File update tasks return existing symbols and `review_ids`; query listed records in bounded pages. Read affected files, reconcile changed and vanished symbols, update precise evidence and summaries, then review relationships and enclosing summaries in the linking task. Previously recorded dependencies determine the initial impact; search the repository to find missing references and add required `new_tasks` when needed. Build configuration changes conservatively requeue included files. Unrelated files do not need rereading merely to report the whole repository complete.

`evidence`, `contexts`, `ports` and `flows` accept optional `freshness` (`current` by default, or `stale`). Stale evidence retains its old hash and line range as a historical claim. Re-read source before replacing it with current evidence. Current entities, relationships, flows and callsites cannot use stale proofs. Completing an update task requires its `review_ids` to be reconciled or explicitly retired; marking only the file read does not clear stale symbols or data.

For a removed symbol or obsolete fact in this task's `review_ids`, add for example `"deletes": {"entities": ["fn:obsolete"], "relations": ["rel:obsolete"]}` to the batch with an explanation in `reason`. Retirement is limited to the task's recorded review scope; file/directory/source inventory can only change through snapshot migration. Dependent memberships, contexts, ports, flows and relations are removed together to prevent broken references. Do not delete and upsert the same identity in one batch. A still-valid symbol retains its ID, and a still-valid data identity retains its color key.

Unique identical-content moves are matched automatically across the old/new inventories. Edited moves can be associated with `renames=[{"from":"old/path.py","to":"new/path.py"}]` in preview and apply; mappings must be one-to-one removed/added paths. Ambiguous same-content copies are not guessed. Matched moves keep existing file/source/symbol/flow identities and layouts; path-dependent callers and imports still require review.

`change_details` explains cosmetic, Python implementation/contract, build, content and unknown changes. Function-level impact uses existing source evidence and call/data relationships; missing evidence or uncertain changes fall back to broader review. Semantic correctness across all languages is not implied.

For history, `blueprint_update(project, action="history")` lists archived revisions. `action="compare", revision=N` returns before/after graph changes, available source-text diffs, today's source reconciliation plan, `current_revision`, `restore_id`, and any restore block reason. Restore using `action="restore", revision=N, expected_revision=current_revision, restore_id=restore_id`. The exact preview must still match. Pause active readers first. Restore archives the current graph, creates a new revision and retains views; it never rewrites repository files. Incompatible evidence is queued for review. The canvas History panel offers the same comparison and restore flow.

Source texts are cached only when exact stored hashes match readable files (up to 2 MiB). Early archives or unreadable/oversize files may lack text; report that absence rather than substitute current content. Graph storage still loads the full document and has no large-repository performance claim.

CLI equivalents:

```text
python -m codemap update MAP
python -m codemap update MAP --apply --expected-revision REVISION --plan-id PLAN_ID
```
