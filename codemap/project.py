"""Local project commands used by a real reader (human or AI) and the canvas."""

from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import uuid4

from .core import apply_batch, completion_report, require, validate_graph
from .repository import scan_repository, source_bytes, decode_text
from .store import GraphStore
from .updates import scan_update, update_plan, impact
from .change_analysis import snapshot_texts


def initialize_project(root, output=None, *, excludes=(), budget=0, langs=None, include=None, legacy_cache=False):
    root = Path(root).resolve(strict=True)
    output = Path(output).resolve() if output else root / '.codemap'
    require(output != root, 'project output cannot replace repository root')
    require(not output.exists() or (output.is_dir() and not any(output.iterdir())), 'output directory must be new or empty; existing projects are never overwritten')
    patterns = list(excludes)
    if output.is_relative_to(root):
        patterns.append(output.relative_to(root).as_posix())
    graph = scan_repository(root, excludes=patterns)
    output.mkdir(parents=True, exist_ok=True)
    store = GraphStore(output / 'map.sqlite')
    store.initialize(graph)
    store.save_source_texts(snapshot_texts(graph))
    from .skeleton import sync
    sync(store, budget=budget, langs=langs, include=include, legacy_cache=legacy_cache)
    return store


def open_project(path):
    database = Path(path) / 'map.sqlite'
    require(database.is_file(), 'project not found: expected a directory containing map.sqlite')
    return GraphStore(database)


def snapshot_status(graph):
    root = graph['project'].get('source_root')
    if not root:
        return {'state': 'unknown', 'reason': 'No repository location in this imported graph.'}
    try:
        fresh = scan_repository(root, excludes=graph.get('inventory', {}).get('exclusion_patterns', []))
        before = {s['path']: s['sha256'] for s in graph['sources']}
        after = {s['path']: s['sha256'] for s in fresh['sources']}
        added = sorted(after.keys() - before.keys())
        deleted = sorted(before.keys() - after.keys())
        changed = sorted(p for p in before.keys() & after.keys() if before[p] != after[p])
        gaps = fresh['inventory']['gaps']
        return {'state': 'changed' if added or deleted or changed or gaps else 'current', 'added': added, 'deleted': deleted, 'changed': changed, 'gaps': gaps}
    except (OSError, ValueError) as error:
        return {'state': 'unavailable', 'reason': str(error)}


def preview_update(store, *, renames=()):
    graph = store.read()
    fresh = scan_update(graph)
    return update_plan(graph, fresh, old_texts=store.source_texts(s['sha256'] for s in graph['sources']),
                       new_texts=snapshot_texts(fresh), renames=renames)


def apply_update(store, *, expected_revision, plan_id, renames=()):
    require(type(expected_revision) is int and expected_revision >= 0 and isinstance(plan_id, str) and plan_id,
            'Apply requires expected_revision and plan_id from a change preview.')
    graph = store.read()
    if graph.get('last_update', {}).get('plan_id') == plan_id:
        require(graph['last_update']['base_revision'] == expected_revision, 'Update plan reused with a different revision.')
        return {'update': graph['last_update'], 'reused': True, **status(store)}
    fresh = scan_update(graph)
    updated = store.update_snapshot(fresh, expected_revision=expected_revision, plan_id=plan_id,
                                    old_texts=store.source_texts(s['sha256'] for s in graph['sources']),
                                    new_texts=snapshot_texts(fresh), renames=renames)
    return {'update': updated.get('last_update'), 'reused': False, **status(store)}


def status(store, *, check_snapshot=False):
    graph = store.read()
    now = time.time()
    counts = {state: sum(t['state'] == state for t in graph['tasks']) for state in ('queued', 'running', 'blocked', 'done')}
    live = [t for t in graph['tasks'] if t['state'] == 'running' and t['lease']['expires_at'] > now]
    result = {'project': graph['project'], 'coverage': completion_report(graph), 'task_counts': counts,
              'execution': 'paused' if graph['project']['execution'] == 'paused' else 'worker_lease_active' if live else 'waiting_for_reader',
              'expired_tasks': [t['id'] for t in graph['tasks'] if t['state'] == 'running' and t['lease']['expires_at'] <= now],
              'inventory': graph.get('inventory', {}), 'last_update': graph.get('last_update')}
    from .skeleton import summary as skeleton_summary
    result['skeleton'] = skeleton_summary(store)
    if check_snapshot:
        result['snapshot'] = snapshot_status(graph)
        if result['snapshot']['state'] != 'current':
            result['coverage']['status'] = 'snapshot_changed'
    return result


def claim_next(store, worker, *, task_id=None, now=None, lease_seconds=1800):
    now = time.time() if now is None else now
    graph = store.read()
    graph = store.recover(expected_revision=graph['project']['revision'], now=now)
    require(graph['project']['execution'] == 'active', 'project is paused')
    tasks = {t['id']: t for t in graph['tasks']}
    ready = [t for t in graph['tasks'] if t['state'] == 'queued' and all(tasks[d]['state'] == 'done' for d in t['depends_on'])]
    paths = {s['id']: s['path'] for s in graph['sources']}
    ready.sort(key=lambda t: (0 if any(paths[s].lower().endswith(('cmakelists.txt', 'readme.md', 'package.json')) for s in t['source_ids']) else 1, [paths[s] for s in t['source_ids']], t['id']))
    if task_id is not None:
        require(task_id in tasks, 'unknown task')
        require(any(t['id'] == task_id for t in ready), 'requested task is not ready')
        ready = [tasks[task_id]]
    if not ready:
        return {'state': 'no_ready_task', 'status': status(store)}
    selected = ready[0]
    graph = store.claim(selected['id'], worker, expected_revision=graph['project']['revision'], now=now, lease_seconds=lease_seconds)
    selected = next(t for t in graph['tasks'] if t['id'] == selected['id'])
    from .structure import task_index
    structure = task_index(store, selected['source_ids'])
    sources = [s for s in graph['sources'] if s['id'] in selected['source_ids']]
    entities = [e for e in graph['entities'] if e['id'] in selected['scope_ids'] or set(e['source_ids']).intersection(selected['source_ids'])]
    review_ids = selected.get('review_ids', {})
    return {'state': 'claimed', 'project': graph['project'], 'task': selected, 'sources': sources, 'entities': entities,
            'review_ids': review_ids, 'update': graph.get('last_update'), 'structure': structure,
            'batch_template': {'format_version': '0.1', 'project_id': graph['project']['id'], 'snapshot_id': graph['project']['snapshot_id'],
                               'base_revision': graph['project']['revision'], 'batch_id': 'batch:' + str(uuid4()), 'task_id': selected['id'],
                               'lease_id': selected['lease']['id'], 'upserts': {}, 'new_tasks': [], 'result': 'partial',
                               'reason': 'Replace with the actual findings and remaining work.'}}


def validate_result(store, batch, *, now=None):
    now = time.time() if now is None else now
    graph = store.read()
    # Pure validation first. No receipt or progress is written on evidence failure.
    proposed = apply_batch(graph, batch, now=now)
    if proposed == graph:
        return graph
    root = graph['project'].get('source_root')
    require(root, 'project has no repository root')
    task = next(t for t in graph['tasks'] if t['id'] == batch['task_id'])
    checked_ids = set(task['source_ids'])
    checked_ids.update(e['source_id'] for e in batch['upserts'].get('evidence', []))
    checked_ids.update(s['id'] for s in batch['upserts'].get('sources', []))
    # Also check existing evidence reused by an incoming reviewed entity/relation.
    proofs = {e['id']: e for e in proposed['evidence']}
    for table in ('entities', 'relations', 'flows', 'contexts', 'ports'):
        for row in batch['upserts'].get(table, []):
            checked_ids.update(proofs[e]['source_id'] for e in row.get('evidence_ids', []))
    for row in batch['upserts'].get('contexts', []):
        if row.get('callsite_evidence_id'):
            checked_ids.add(proofs[row['callsite_evidence_id']]['source_id'])
    counts = {}
    for source_id in checked_ids:
        _, raw = source_bytes(proposed, root, source_id)
        counts[source_id] = max(1, len(decode_text(raw)[0].splitlines()))
    for proof in proposed['evidence']:
        if proof['source_id'] in counts and proof.get('freshness') != 'stale':
            require(proof['end_line'] <= counts[proof['source_id']], f"{proof['id']}: evidence range exceeds file")
    if task['id'] == 'task:repository-review' and batch['result'] == 'done':
        require(snapshot_status(proposed)['state'] == 'current', 'repository inventory changed; repository review cannot complete')
    return proposed


def commit_result(store, batch, *, now=None):
    now = time.time() if now is None else now
    validate_result(store, batch, now=now)
    return store.commit(batch, now=now)


def canvas_graph(store):
    graph = deepcopy(store.read())
    snapshot = snapshot_status(graph)
    affected = set(snapshot.get('changed', []) + snapshot.get('deleted', []))
    if snapshot['state'] in {'unknown', 'unavailable'} or snapshot.get('gaps'):
        affected.update(s['path'] for s in graph['sources'])
    source_ids = {s['id'] for s in graph['sources'] if s['path'] in affected}
    affected_ids = impact(graph, source_ids)
    for name, ids in affected_ids.items():
        if name != 'sources':
            for row in graph[name]:
                if row['id'] in ids:
                    row['freshness'] = 'stale'
    current_status = status(store)
    current_status['coverage'] = completion_report(graph)
    if snapshot['state'] != 'current':
        current_status['coverage']['status'] = 'snapshot_changed'
    from .structure import cached, project_structure, index_status
    rows = {id: row for id, row in cached(store, graph).items() if id not in source_ids}
    # Coverage is deliberately calculated from saved review records before projection.
    structure = index_status(store, graph, unavailable_sources=source_ids)
    return {'graph': project_structure(graph, rows), 'status': current_status, 'snapshot': snapshot, 'structure': structure}


def export_graph(store, output):
    output = Path(output)
    require(not output.exists(), 'export target already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(store.read(), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return str(output.resolve())
