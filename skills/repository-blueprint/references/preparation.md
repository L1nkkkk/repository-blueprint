# Efficient reading and prepared batches

These helpers reduce repeated reading and JSON authoring. They preserve the entire required queue and original evidence/coverage checks; they do not establish semantic truth.

## Reading cycle

1. Use `blueprint_status(project, check_snapshot=true)` when starting/resuming and before delivery. Default `summary` preserves exact coverage status and task counts; list summaries contain `count`, `sample`, `truncated`. Use `detail="full"` for complete status. Intermediate status calls can use `check_snapshot=false`; commits still check their relevant files, but this option does not verify the entire inventory.
2. Claim one task with `blueprint_next`. Keep the exact empty `batch_template`. Default task/source/entity lists are bounded, and entity summaries are NOT complete upserts. Fetch the full task using `blueprint_query(table="tasks", ids=[task_id])` when lists are truncated, then query its source/scope/review IDs in pages. Do not call next again to obtain more detail: it claims another task. `next(detail="full")` is available at initial acquisition.
3. Read the `reading_pack` attached to the claim, then continue with `blueprint_pack(project, task_id, cursor=next_cursor)`. The pack groups actual source, structure, full saved records and pending work. Delivered complete lines need no duplicate read; complete records with `reuse="current_evidence"` need no duplicate full query. Follow gaps and structural continuation pointers. Use `blueprint_context`/query for material missing from the pack; a context summary still needs full details by ID. This checks only recorded dependencies, not new call sites or every runtime branch. See [reading packs](reading-pack.md).
4. Fully read the current task's included files and enumerate declarations. Consult current saved callee/interface findings before rereading another file. Read precise ranges for new claims, changed interfaces or unresolved relations. Describe a document's role/content without auditing every implementation promise unless requested or a specific conflict warrants it. Preserve concrete required follow-ups for missing cross-file work. This never excuses omitting repository files or function-level details.
5. Check the `preparation_contract` attached to the claim, prepare concise findings, inspect the full draft if needed, and commit its ID. On a rejected draft, fix the listed independent issues together; retain this connection and its already read material. Continue the required queue. Read only needed guide sections; `guide.version` identifies identical content within a session.

## Draft contract

Call `blueprint_prepare(project, batch_template, upserts, result, reason, new_tasks?, deletes?, detail?)`. It compiles and validates a complete immutable batch. The original graph format applies to semantic fields. The reader must supply actual findings and an explicit result; the tool never decides completion.

- Existing records: exact `id` plus changed fields. The tool merges the complete current saved record; arrays replace entirely, never append implicitly. Source path/hash/category/inclusion cannot change. New records use `key` instead of `id`, unique throughout the draft. IDs derive from project, task, table and key; after committing, update by saved ID.
- References: `$key` in ID fields (`source_ids`, `evidence_ids`, `parent_id`, `child_id`, `entity_id`, `context_id`, `caller_id`, `callee_id`, `callsite_evidence_id`, `producer_port_id`, `from_id`, `to_id`, `flow_id`, `derived_from`, task `scope_ids`/`depends_on`). Literal existing IDs work too. Summary text is never interpreted as a reference.
- Evidence: `source` (relative path or inventory ID), `start_line`, `end_line`, `note`. The tool fills source ID and SHA only for ranges actually returned by `blueprint_read` or complete `source` items in `reading_pack` on this connection at this snapshot/hash. Paginated reads can cover a range; gaps fail. Index outlines, search and saved-context lookup do not count as reading. Reuse existing proof IDs without copying their records. Replacing stale evidence requires rereading its cited lines.
- Sources: `source` or existing `id`, and explicit `read_state` plus `symbols_complete`. Newly marking read/indexed requires all lines returned by this connection's reads. Previously saved read state can be reused while its fingerprint matches; it does not establish semantics. Empty text has one blank logical line.
- Entities: name, kind, sources, responsibility and actual analysis status. New entities default to partial. Reviewed functions/methods require explicit details arrays for inputs, outputs, calls, reads, writes and conditions; the tool never invents empty semantic arrays. Language can come from sources; roles default empty and freshness current. Existing stale entities need explicit reconciliation/current evidence.
- Contexts, ports, flows, relations, memberships: supply semantic fields from the graph format. IDs/references are mechanical. A new flow may omit color_key, but must specify its value version, producer and derivation. Multiple inputs remain independent values in their scenario context.
- New tasks: key, kind, scope/source references, dependencies and concrete reason. Queued state, zero attempts, null lease and required=true are filled. Retirements use existing IDs and the original update-task restrictions.

## Completion checklist and repair feedback

For analyze/update tasks, `result="done"` requires **every task scope node** (including file/module nodes, not just newly described functions) to have `analysis="reviewed"` and `freshness="current"`. Every task source must have `read_state="read"`; source-category files also require `symbols_complete=true`. These flags are explicit findings, never an instruction to mark unfinished work done. Keep `result="partial"` with a concrete continuation reason when work remains.

`next.preparation_contract.function_details_template` contains all six required keys with **null placeholders**. Fill every key from the actual reading before marking a function reviewed. Null/missing is unfinished; `[]` is an explicit verified absence. Supplying a details object replaces that object, so retain its other valid arrays when fixing one field.

Prepare errors remain MCP `isError=true`. Their text is JSON, also provided as structuredContent to supported clients:

```json
{
  "error": "validation_failed",
  "stage": "findings_and_completion",
  "issues": [
    {"code": "function_details_array", "category": "validation", "record_name": "echo", "field": "details.conditions", "current": {"present": false}, "expected": "array", "action": "Fill from existing findings; [] only after verifying absence."},
    {"code": "scope_analysis", "category": "validation", "record_name": "echo.py", "field": "analysis", "current": {"present": true, "type": "string", "value": "partial"}, "expected": "reviewed", "action": "Reconcile the required file node as well as its functions, or retain partial with remaining work."}
  ]
}
```

Read `field`, `current`, `expected`, source paths and `action`; repair findings already supported by the reading. When identifiable, `input_path` and `local_key` point back to the submitted record, including new records and same-name functions. A field/status error is not a demand to reread the repository. Reading errors identify `requires_read=true` and, when known, exact `missing_ranges`; read only those gaps on the same connection. Keep valid evidence IDs, fields and prepared drafts. Do not repeatedly send an unchanged rejected request.

Independent finding, scope-state and source-reading problems are collected together when compilation permits it. Identity/reference errors stop dependent checks safely. Responses include `pending_checks`, `issue_count` and `truncated`; at most 40 issue details are returned. Fix the listed problems, then run prepare again to check the remaining dependencies. This is not a claim to diagnose every possible invalid graph in one pass. Rejection does not save a prepared draft or advance graph progress.

## Complete function example

Suppose `echo.py` is exactly `def echo(value):` followed by an indented `return value`, and both lines were returned on this connection. With the original empty batch_template and the actual file node ID from the claim, the concise findings are:

```json
{
  "upserts": {
    "sources": [{"key": "source", "source": "echo.py", "read_state": "read", "symbols_complete": true}],
    "evidence": [{"key": "body", "source": "echo.py", "start_line": 1, "end_line": 2, "note": "Function returns its value parameter unchanged."}],
    "entities": [
      {"id": "actual-file-id-from-next", "key": "file", "analysis": "reviewed", "summary": "Defines echo(value), which returns its input.", "evidence_ids": ["$body"]},
      {"key": "echo", "kind": "function", "name": "echo", "source_ids": ["$source"], "analysis": "reviewed", "summary": "Returns the supplied value unchanged.", "evidence_ids": ["$body"], "details": {"inputs": ["value"], "outputs": ["value"], "calls": [], "reads": ["value parameter"], "writes": [], "conditions": []}}
    ],
    "memberships": [{"key": "member", "parent_id": "$file", "child_id": "$echo", "axis": "semantic"}]
  },
  "result": "done",
  "reason": "Both lines, the file summary and the function declaration have been reviewed."
}
```

This example's empty arrays are justified by that specific two-line function. Do not copy them into findings for other functions without checking their behavior.

## One-line module example

Suppose `constants.py` contains only `VALUE = 1`, was fully read through MCP, and `file-id-from-next` is its saved file entity. Pass project, the original empty template, and:

```json
{
  "upserts": {
    "sources": [{"key": "file", "source": "constants.py", "read_state": "read", "symbols_complete": true}],
    "evidence": [{"key": "assignment", "source": "constants.py", "start_line": 1, "end_line": 1, "note": "Literal integer assignment to VALUE."}],
    "entities": [{"id": "file-id-from-next", "analysis": "reviewed", "summary": "Exports the integer constant VALUE; no function/class declarations.", "evidence_ids": ["$assignment"]}]
  },
  "result": "done",
  "reason": "Full one-line module and declarations checked; repository linking remains queued."
}
```

Prepare returns prepared_id, counts, the first 20 local ID mappings and committed=false. The draft is saved separately from graph progress. Inspect the complete batch with `blueprint_prepare(project, prepared_id, detail="full")`; commit with `blueprint_commit(project, prepared_id)` without resending its body.

Drafts survive disconnects. A new connection can commit an existing draft if lease, revision and files still match, but does not inherit the previous connection's read ledger for authoring new evidence. Prepare does not extend a lease. Commit rechecks the original protocol, source hashes and line bounds.

After source/revision/lease changes, prepare a new draft with current identities; never patch saved hashes to bypass validation. Renewal advances revision, so update template base_revision and use a new batch ID when changing payload. Retry an uncertain successful commit with the same prepared_id to avoid double counting. Failed prepare/commit does not advance progress. Inspection shows the original draft, not a promise it remains committable.

Raw `blueprint_commit(project, batch)` and CLI remain available. Raw upserts are complete records; concise merges and aliases belong only to prepare. Commit accepts exactly one of batch or prepared_id.
