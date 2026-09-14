# Local structural index

`blueprint_index` is shared by all MCP hosts. It extracts syntax without executing the target repository. Python uses the standard-library AST; C++, C#, TypeScript/TSX and JavaScript/JSX use optional, pinned Tree-sitter packages. Missing backends and syntax errors are explicit gaps. Other inventoried languages still have required reading tasks.

New maps persist all eligible files in the relational skeleton during init; no model is invoked. `blueprint_next` automatically indexes the claimed source files and returns a compact symbol list. For a whole repository call `blueprint_index(action="build", project=...)`; follow `next_offset` with the returned `snapshot_id` until null. The canvas “解析结构” button and CLI `index PROJECT` do this automatically. Hashes and parser versions key the cache; unchanged files are not reparsed. Source edits must first be registered through `blueprint_update`.

Query one source with `blueprint_index(action="query", source="relative/path.py", project=...)`. Page with `offset` and the returned `index_id`. Default table `symbols` includes IDs, lexical parents, declaration kinds, signatures, parameters and exact line ranges. `table="sites"` lists syntactic calls/imports with `resolution="unresolved"`; it does not resolve callee identity, establish call contexts or prove data flows. `table="diagnostics"` supplies bounded parser diagnostics. A `parsed` state means the backend reported no syntax recovery, not that all runtime behavior is known. Limits, missing backends, unsupported languages and parse recovery never count as review completion.

Read actual numbered code through `blueprint_read`. Its `source` accepts an inventory source/path or a `symbol:` ID, starting at that declaration when `start` is omitted. Follow `next_start` to cover the task's entire file; an index query never satisfies the read ledger.

## Reuse generated structure when preparing findings

In `blueprint_prepare`, an entity can reference `symbol_id` instead of repeating kind/name/source IDs, signature, language and parent memberships. Parents are filled automatically as located nodes; review their responsibilities too. A local `key` can still be used by other records in the same batch. Existing unambiguous matching entities retain their identities; ambiguous overloads are not merged. Evidence can reference `symbol_id` to fill its source, hash and full declaration range. Explicit smaller `start_line`/`end_line` ranges are supported when appropriate; the read ledger must cover the actual chosen range.

Example `upserts` fragment (replace the symbol ID with one returned for the current file):

```json
{
  "evidence": [{"key": "body", "symbol_id": "symbol:...", "note": "Returns the supplied value unchanged."}],
  "entities": [{"key": "fn", "symbol_id": "symbol:...", "analysis": "reviewed",
    "summary": "Returns the supplied value unchanged.", "evidence_ids": ["$body"],
    "details": {"inputs": ["value"], "outputs": ["value"], "calls": [], "reads": [], "writes": [], "conditions": []}}]
}
```

Supply an exact empty `batch_template`, explicit result/reason, file/source review updates and any additional semantic facts as usual. Prepared drafts and commits keep the normal revision, lease, evidence and analysis-depth checks. The parser never fills semantic details, marks a source read, changes `symbols_complete`, ends a task, or creates flows.

Structural cache and canvas nodes are separate from saved semantic records. `blueprint_query` and graph JSON export contain committed records; query the index for unreviewed syntax. The canvas combines both and labels generated nodes “结构已解析”. Promoted nodes are saved with the reviewed findings at commit. Indexed parameters are declaration metadata in node details, not invented data ports.

This version parses changed files again and reuses unchanged file results. It does not yet reuse syntax subtrees across edits, run Clang/Roslyn/TypeScript type checkers, expand UE macros or resolve dynamic dispatch. Declaration identities omit bodies and line numbers; changed signatures/names and indistinguishable duplicate declarations can change identity. Normal snapshot review remains necessary.

Native backends run in a separate process with a 15-second per-file timeout; a crash or timeout reports an unavailable file and indexing continues. Python syntax stays in process. Cache hits do not launch a parser. The pinned Tree-sitter 0.25.2 backend passed the real canvas-source regression on Windows; 0.26.0 produced a native access violation during that check and is not the validated dependency.


## Persistent skeleton and budgeted summaries

`blueprint_init` accepts `budget` (default 0), `langs` and `include`; `blueprint_sync(project, budget=...)` reconciles dirty/untracked files as well as committed changes. Query `blueprint_find_symbol(name=...)`, `blueprint_callers(symbol=..., depth<=3)`, `blueprint_callees`, `blueprint_search(query=...)`, and `blueprint_repo_map(budget_tokens=...)` without a lease. Names can be ambiguous; use exact IDs. Edges are syntax candidates with confidence, never runtime proof. Every returned summary includes current/stale/suspect, or semantics=null when absent. Check status and synchronize external source changes before reuse. `blueprint_doctor` checks relational/FTS/hash consistency.

`blueprint_search` now defaults to FTS5 names/signatures/summaries. To use the earlier literal source search, pass `paths="*"` (or a narrower glob). CLI search remains literal source search.

For optional node summaries, set a budget through sync, then call `blueprint_semantic_claim(project, worker, n=1, lease_seconds=1800)`. It returns exact source slices, node_id/body_sha/lease_id, line ranges, syntax context and estimated_tokens. Read the source, constrain actual token use to the reservation, and submit with `blueprint_semantic_submit(project, batch={batch_id, results})`. Each result must include node_id, body_sha, lease_id, nonempty summary, detail object, nonempty evidence array, model identity and used_tokens. Each evidence item needs the same node_id/body_sha and start_line/end_line inside the node. Retry an uncertain submission with the identical batch ID and contents. The transaction rejects stale disk bytes, expired/replaced leases, bad evidence and declared usage beyond reservation. No model process is launched; any host can execute this protocol.

This queue is separate from original next/prepare/commit full-blueprint work and never marks whole-file review complete. Skeleton sync does not apply the legacy graph update: continue using preview/apply update for the original evidence graph. Native name resolution, full-tree hash reconciliation, and conservative token estimates remain explicit limitations. Exact file renames preserve historical symbol IDs; freshly rebuilt projects can use different IDs after renames.
