"""Snapshot migration and conservative impact analysis of recorded dependencies.

This schedules reading; it does not infer new code semantics or execute an AI.
"""

from collections import defaultdict, deque
from copy import deepcopy

from .core import DATA_RELATIONS, digest, require, validate_graph
from .repository import scan_repository, task, identity


FACT_TABLES = ('entities', 'memberships', 'contexts', 'ports', 'flows', 'relations', 'evidence')


def retired_closure(graph, removed):
    """Find records that cannot survive removed owners, contexts or producers."""
    removed = {name: set(removed.get(name, ())) for name in ('sources', *FACT_TABLES)}
    changed = True
    while changed:
        changed = False
        for name in FACT_TABLES:
            for row in graph[name]:
                if row['id'] in removed[name]:
                    continue
                invalid = False
                if name == 'entities':
                    invalid = bool(row['source_ids']) and set(row['source_ids']) <= removed['sources']
                elif name == 'evidence':
                    invalid = row['source_id'] in removed['sources']
                elif name == 'memberships':
                    invalid = row['parent_id'] in removed['entities'] or row['child_id'] in removed['entities']
                elif name == 'contexts':
                    invalid = (row.get('parent_id') in removed['contexts'] or row.get('caller_id') in removed['entities']
                               or row.get('callee_id') in removed['entities'] or row.get('callsite_evidence_id') in removed['evidence'])
                elif name == 'ports':
                    invalid = row['entity_id'] in removed['entities'] or row['context_id'] in removed['contexts']
                elif name == 'flows':
                    invalid = row['producer_port_id'] in removed['ports'] or bool(set(row['derived_from']) & removed['flows'])
                elif name == 'relations':
                    owners = removed['ports'] if row['kind'] in DATA_RELATIONS else removed['entities']
                    invalid = (row['from_id'] in owners or row['to_id'] in owners or row['context_id'] in removed['contexts']
                               or row.get('flow_id') in removed['flows'])
                if invalid:
                    removed[name].add(row['id'])
                    changed = True
    return removed


def remove_records(graph, removed):
    for name, ids in removed.items():
        graph[name] = [row for row in graph[name] if row['id'] not in ids]
    for row in graph['entities']:
        old = row['evidence_ids']
        row['source_ids'] = [s for s in row['source_ids'] if s not in removed.get('sources', ())]
        row['evidence_ids'] = [e for e in old if e not in removed.get('evidence', ())]
        if old != row['evidence_ids']:
            row['freshness'] = 'stale'
            if not row['evidence_ids']:
                row['analysis'] = 'partial'
    for row in graph['flows'] + graph['relations']:
        old = row['evidence_ids']
        row['evidence_ids'] = [e for e in old if e not in removed.get('evidence', ())]
        if old != row['evidence_ids']:
            row['freshness'] = 'stale'
            if 'basis' in row and not row['evidence_ids']:
                row.update(basis='unresolved', reason='Previous source evidence was removed; recheck this relationship.')
    for row in graph['tasks']:
        row['scope_ids'] = [e for e in row['scope_ids'] if e not in removed['entities']]
        row['source_ids'] = [s for s in row['source_ids'] if s not in removed.get('sources', ())]


def impact(graph, source_ids, *, precise_seeds=None):
    """Propagate at file granularity; group ancestors are summaries, not dependencies."""
    entities = {e['id']: e for e in graph['entities']}
    proofs = {e['id']: e['source_id'] for e in graph['evidence']}
    owners = {p['id']: p['entity_id'] for p in graph['ports']}
    producer = {f['id']: owners[f['producer_port_id']] for f in graph['flows']}
    links = defaultdict(set)
    by_source = defaultdict(set)
    entity_sources = {}
    for e in entities.values():
        ids = set(e['source_ids']) | {proofs[p] for p in e['evidence_ids']}
        entity_sources[e['id']] = ids
        # Repository/directory summaries may cite shared build evidence. Re-read
        # these in the link task without pulling unrelated files into the queue.
        if e['kind'] not in {'repository', 'directory', 'module', 'package'}:
            for s in ids:
                by_source[s].add(e['id'])
    for r in graph['relations']:
        a, b = (owners[r['from_id']], owners[r['to_id']]) if r['kind'] in DATA_RELATIONS else (r['from_id'], r['to_id'])
        if r['kind'] in DATA_RELATIONS or r['kind'] in {'control', 'event'}:
            links[a].add(b)
        if r['kind'] not in DATA_RELATIONS:
            links[b].add(a)
        for p in r['evidence_ids']:
            by_source[proofs[p]].update((a, b))
    for c in graph['contexts']:
        if c.get('callee_id'):
            links[c['callee_id']].add(c['caller_id'])
            by_source[proofs[c['callsite_evidence_id']]].update((c['caller_id'], c['callee_id']))
    for f in graph['flows']:
        for parent in f['derived_from']:
            links[producer[parent]].add(producer[f['id']])
        for p in f['evidence_ids']:
            by_source[proofs[p]].add(producer[f['id']])
    affected_sources, affected_entities = set(source_ids), set()
    # Changes to build configuration can change compilation/import resolution.
    if any(s['id'] in source_ids and s['category'] == 'build' for s in graph['sources']):
        affected_sources.update(s['id'] for s in graph['sources'] if s['included'])
    precise_seeds = precise_seeds or {}
    queue = deque(e for s in affected_sources for e in precise_seeds.get(s, by_source[s]))
    selective = bool(precise_seeds)
    while queue:
        id = queue.popleft()
        if id in affected_entities:
            continue
        affected_entities.add(id)
        queue.extend(links[id] - affected_entities)
        for s in entity_sources[id] - affected_sources:
            affected_sources.add(s)
            if not selective:
                queue.extend(by_source[s] - affected_entities)
    # Shared source-backed summaries become stale without expanding siblings.
    affected_evidence = {e['id'] for e in graph['evidence'] if e['source_id'] in affected_sources}
    affected_entities.update(e['id'] for e in entities.values() if set(e['evidence_ids']) & affected_evidence)
    # All enclosing summaries need review, without expanding their siblings.
    parents = defaultdict(set)
    for m in graph['memberships']:
        parents[m['child_id']].add(m['parent_id'])
    queue = deque(affected_entities)
    while queue:
        for parent in parents[queue.popleft()] - affected_entities:
            affected_entities.add(parent)
            queue.append(parent)
    affected_contexts = {c['id'] for c in graph['contexts'] if c.get('caller_id') in affected_entities
                         or c.get('callee_id') in affected_entities or c.get('callsite_evidence_id') in affected_evidence
                         or set(c.get('evidence_ids', ())) & affected_evidence}
    affected_ports = {p['id'] for p in graph['ports'] if p['entity_id'] in affected_entities or p['context_id'] in affected_contexts
                      or set(p.get('evidence_ids', ())) & affected_evidence}
    affected_flows = {f['id'] for f in graph['flows'] if f['producer_port_id'] in affected_ports or set(f['evidence_ids']) & affected_evidence}
    affected_endpoints = affected_ports | affected_entities
    affected_relations = {r['id'] for r in graph['relations'] if r['context_id'] in affected_contexts
                          or r['from_id'] in affected_endpoints or r['to_id'] in affected_endpoints
                          or r.get('flow_id') in affected_flows or set(r['evidence_ids']) & affected_evidence}
    return {'sources': affected_sources, 'entities': affected_entities, 'evidence': affected_evidence,
            'contexts': affected_contexts, 'ports': affected_ports, 'flows': affected_flows, 'relations': affected_relations}


def scan_update(graph):
    root = graph['project'].get('source_root')
    require(root, 'Project has no repository location; set up a local project first.')
    return scan_repository(root, excludes=graph.get('inventory', {}).get('exclusion_patterns', []))


def update_plan(graph, fresh, *, old_texts=None, new_texts=None, renames=()):
    from .change_analysis import exact_renames, classify_change
    before = {s['path']: s for s in graph['sources']}
    after = {s['path']: s for s in fresh['sources']}
    added, deleted = sorted(after.keys() - before.keys()), sorted(before.keys() - after.keys())
    matches = exact_renames(before, after)
    require(isinstance(renames, (list, tuple)), 'renames must be a list')
    used_old, used_new = set(), set()
    for pair in renames:
        require(isinstance(pair, dict) and set(pair) == {'from', 'to'}, 'rename pair requires from and to')
        require(pair['from'] in deleted and pair['to'] in added, 'rename must match a removed path to an added path')
        require(pair['from'] not in used_old and pair['to'] not in used_new, 'rename mapping must be one-to-one')
        used_old.add(pair['from']); used_new.add(pair['to'])
        matches = [m for m in matches if m['from'] != pair['from'] and m['to'] != pair['to']]
        matches.append({**pair, 'basis': 'explicit_mapping'})
    matches.sort(key=lambda m: m['from'])
    added = [p for p in added if p not in {m['to'] for m in matches}]
    deleted = [p for p in deleted if p not in {m['from'] for m in matches}]
    changed = sorted(p for p in before.keys() & after.keys() if any(before[p].get(k) != after[p].get(k)
                     for k in ('sha256', 'included', 'category', 'encoding')))
    details = [classify_change(graph, before[p], (old_texts or {}).get(before[p]['sha256']),
                              (new_texts or {}).get(after[p]['sha256'])) for p in changed]
    seeds = {}
    for detail in details:
        source_id = before[detail['path']]['id']
        if detail['kind'] == 'cosmetic' and before[detail['path']]['category'] != 'build':
            seeds[source_id] = {e['id'] for e in graph['entities'] if e['kind'] == 'file' and source_id in e['source_ids']}
        elif detail['kind'] == 'implementation':
            seeds[source_id] = set(detail['entity_ids'])
    # A path change can affect imports/build resolution, even with identical bytes.
    changed_ids = {before[p]['id'] for p in changed + deleted + [m['from'] for m in matches]}
    affected = impact(graph, changed_ids - seeds.keys())
    for source_id, ids in seeds.items():
        narrow = impact(graph, {source_id}, precise_seeds={source_id: ids})
        for name in affected:
            affected[name].update(narrow[name])
    if any(after[p]['category'] == 'build' for p in added):
        affected = impact(graph, {s['id'] for s in graph['sources'] if s['included']})
    def inventory_signature(value):
        value = deepcopy(value or {})
        value['excluded_paths'] = [p for p in value.get('excluded_paths', [])
                                   if p['path'].split('/')[-1] not in {'.git', '.hg', '.svn', '.codemap'}]
        return value
    inventory_changed = inventory_signature(graph.get('inventory')) != inventory_signature(fresh['inventory'])
    plan = {'project_id': graph['project']['id'], 'base_revision': graph['project']['revision'],
            'from_snapshot': graph['project']['snapshot_id'], 'to_snapshot': fresh['project']['snapshot_id'],
            'added': added, 'changed': changed, 'deleted': deleted, 'renamed': matches,
            'rename_approvals': list(renames), 'change_details': details, 'inventory_changed': inventory_changed,
            'gaps': fresh['inventory']['gaps'], 'can_apply': not fresh['inventory']['gaps'],
            'affected_paths': sorted({next((m['to'] for m in matches if m['from'] == p), p) for p in before if before[p]['id'] in affected['sources']} | set(added)),
            'affected_ids': {key: sorted(ids) for key, ids in affected.items()},
            'state': 'changed' if added or changed or deleted or matches or inventory_changed else 'current',
            'scope_note': 'Syntax-equivalent changes and located Python implementation changes use narrower dependency seeds. Unknown changes and build changes remain conservative. Missing dependencies still require reader search. Renames preserve identities; ambiguous or edited moves require an explicit mapping.'}
    plan['plan_id'] = digest(plan)
    return plan


def migrate_snapshot(graph, fresh, *, expected_revision, plan_id, old_texts=None, new_texts=None, renames=()):
    require(type(expected_revision) is int and expected_revision == graph['project']['revision'], 'stale project revision; preview changes again')
    plan = update_plan(graph, fresh, old_texts=old_texts, new_texts=new_texts, renames=renames)
    require(plan['can_apply'], 'Inventory has gaps; resolve them before updating. The saved graph is unchanged.')
    require(plan_id == plan['plan_id'], 'Source or plan changed; preview changes again')
    if plan['state'] == 'current':
        return deepcopy(graph)
    updated = deepcopy(graph)
    old_sources = {s['path']: s for s in graph['sources']}
    renamed_sources = {m['to']: old_sources[m['from']] for m in plan['renamed']}
    old_sources.update(renamed_sources)
    affected = {k: set(v) for k, v in plan['affected_ids'].items()}
    removed = retired_closure(updated, {'sources': {old_sources[p]['id'] for p in plan['deleted']}})
    remove_records(updated, removed)
    # Existing source identities survive same-path edits, including imported IDs.
    source_map = {s['id']: old_sources.get(s['path'], s)['id'] for s in fresh['sources']}
    current_sources = []
    for source in fresh['sources']:
        old = old_sources.get(source['path'])
        row = deepcopy(old if old and old['id'] not in affected['sources'] else source)
        row['id'] = source_map[source['id']]
        row['path'] = source['path']
        current_sources.append(row)
    updated['sources'] = current_sources
    existing = {e['id'] for e in updated['entities']}
    fresh_file_map = {}
    for row in fresh['entities']:
        if row['kind'] == 'file':
            ids = [source_map[s] for s in row['source_ids']]
            old = next((e for e in updated['entities'] if e['kind'] == 'file' and e['source_ids'] == ids), None)
            fresh_file_map[row['id']] = old['id'] if old else row['id']
            if old:
                old.update(name=row['name'], source_ids=ids, language=row['language'], roles=row['roles'])
        if row['id'] not in existing and fresh_file_map.get(row['id'], row['id']) not in existing:
            row = deepcopy(row)
            row['source_ids'] = [source_map[s] for s in row['source_ids']]
            updated['entities'].append(row)
            existing.add(row['id'])
    # Physical memberships are scan facts, not AI conclusions.
    updated['memberships'] = [m for m in updated['memberships'] if m['axis'] != 'physical']
    for row in fresh['memberships']:
        row = deepcopy(row)
        row['child_id'] = fresh_file_map.get(row['child_id'], row['child_id'])
        row['parent_id'] = fresh_file_map.get(row['parent_id'], row['parent_id'])
        updated['memberships'].append(row)
    # Retire vanished physical folders; independent semantic groups stay visible.
    fresh_dirs = {e['id'] for e in fresh['entities'] if e['kind'] in {'directory', 'repository'}}
    gone_dirs = {e['id'] for e in updated['entities'] if e['kind'] == 'directory' and e['id'] not in fresh_dirs}
    if gone_dirs:
        remove_records(updated, retired_closure(updated, {'entities': gone_dirs}))
    for name, ids in affected.items():
        if name != 'sources':
            for row in updated[name]:
                if row['id'] in ids:
                    row['freshness'] = 'stale'
    # Any evidence with an old content hash is historical until explicitly reread.
    hashes = {s['id']: s['sha256'] for s in updated['sources']}
    for row in updated['evidence']:
        if row['sha256'] != hashes[row['source_id']]:
            row['freshness'] = 'stale'
    stale_proofs = {e['id'] for e in updated['evidence'] if e.get('freshness') == 'stale'}
    for name in ('entities', 'contexts', 'flows', 'relations'):
        for row in updated[name]:
            if set(row.get('evidence_ids', ())) & stale_proofs or row.get('callsite_evidence_id') in stale_proofs:
                row['freshness'] = 'stale'
    source_ids = {s['id'] for s in updated['sources']}
    for t in updated['tasks']:
        if t['state'] == 'running':
            t.update(state='queued', lease=None, reason='Source snapshot advanced; continue with a new reading lease.')
        if t['id'] == 'task:inventory-gaps':
            t.update(state='done', lease=None, reason='Inventory rescanned without gaps.')
        if t['kind'] in {'analyze', 'update'} and not t['source_ids'] and not t['scope_ids']:
            t.update(state='done', lease=None, reason='Source removed in the new snapshot; previous facts archived.')
    tasks = {t['id']: t for t in updated['tasks']}
    for source in updated['sources']:
        if not source['included'] or not (source['id'] in affected['sources'] or source['path'] in plan['added']):
            continue
        scope = [e['id'] for e in updated['entities'] if source['id'] in e['source_ids'] and e['kind'] != 'external']
        id = next((t['id'] for t in updated['tasks'] if t['kind'] in {'analyze', 'update'} and t['source_ids'] == [source['id']]), identity('task', source['path']))
        previous = tasks.get(id)
        row = task(id, 'update', scope, [source['id']], reason='Read this affected file and reconcile existing symbols with the new source.', blocked=not source.get('encoding'))
        row['attempt'] = previous['attempt'] if previous else 0
        row['review_ids'] = {'entities': scope, 'evidence': [e['id'] for e in updated['evidence'] if e['source_id'] == source['id']]}
        tasks[id] = row
    summaries = [e for e in updated['entities'] if not e['source_ids'] and e['kind'] != 'external']
    for e in summaries:
        # A new/deleted file also changes the repository's completeness claim.
        if plan['added'] or plan['deleted'] or plan['inventory_changed']:
            e['freshness'] = 'stale'
    link = tasks.get('task:repository-review', task('task:repository-review', 'link', [], []))
    pending_ids = affected['sources'] & source_ids | {s['id'] for s in updated['sources'] if s['path'] in plan['added']}
    pending_ids.update(s for t in tasks.values() if t['id'] != link['id'] and t['state'] != 'done' for s in t['source_ids'])
    link.update(state='queued', lease=None, scope_ids=[e['id'] for e in summaries if e['freshness'] == 'stale' or e['analysis'] != 'reviewed'],
                source_ids=sorted(pending_ids), depends_on=[id for id in tasks if id != link['id']],
                reason='Recheck affected calls, inputs, derived values and enclosing summaries. Search for dependencies missing from the old graph.')
    link['review_ids'] = {name: [r['id'] for r in updated[name] if r.get('freshness') == 'stale'] for name in FACT_TABLES}
    tasks[link['id']] = link
    updated['tasks'] = list(tasks.values())
    updated['inventory'] = deepcopy(fresh['inventory'])
    updated['project'].update(snapshot_id=plan['to_snapshot'], inventory_complete=True, revision=graph['project']['revision'] + 1)
    updated['last_update'] = {**plan, 'applied_revision': updated['project']['revision'],
                              'archived_revision': graph['project']['revision'], 'removed_ids': {k: sorted(v) for k, v in removed.items() if v}}
    return validate_graph(updated)
