"""Separate reading coverage, recorded scenarios and known analysis gaps."""
from .core import DATA_RELATIONS, completion_report


def coverage_report(graph):
    summary = completion_report(graph)
    entities = {e['id']: e for e in graph['entities']}
    contexts = {c['id']: c for c in graph['contexts']}
    ports = {p['id']: p for p in graph['ports']}
    roots = {}
    for context in graph['contexts']:
        root = context
        while root.get('parent_id'):
            root = contexts[root['parent_id']]
        roots[context['id']] = root['id']
    def owner(context):
        child = next((c for c in graph['contexts'] if c.get('parent_id') == context['id']), None)
        if child:
            return child['caller_id']
        ids = {p['entity_id'] for p in graph['ports'] if p['context_id'] == context['id']}
        return next((e['id'] for e in graph['entities'] if e['id'] in ids and e['kind'] in {'function', 'method'}), None)
    groups = {c['id']: {'id': c['id'], 'entity_id': owner(c), 'name': c.get('label') or c['id'],
                      'entities': set(), 'relations': [], 'context_ids': []} for c in graph['contexts'] if not c.get('parent_id')}
    for context in graph['contexts']:
        group = groups[roots[context['id']]]
        group['context_ids'].append(context['id'])
        group['entities'].update(value for key, value in context.items() if key in {'root_entity_id', 'caller_id', 'callee_id'} and value in entities)
    for relation in graph['relations']:
        group = groups.get(roots.get(relation.get('context_id')))
        if group is None:
            continue
        group['relations'].append(relation)
        group['entities'].update(ports[p]['entity_id'] if p in ports else p for p in (relation['from_id'], relation['to_id']))
    scenarios, in_scenarios = [], set()
    for group in groups.values():
        if not group['relations'] or not group['entity_id']:
            continue  # A scope declaration alone is not a drawn flow.
        in_scenarios.update(group['entities'])
        root = entities.get(group['entity_id'], {})
        pending = sum(r['basis'] != 'source' or r.get('freshness', 'current') != 'current' for r in group['relations'])
        scenarios.append({'id': group['id'], 'entity_id': group['entity_id'], 'name': root.get('name') or group['name'],
                          'purpose': root.get('summary', ''), 'entity_count': len(group['entities']),
                          'relation_count': len(group['relations']), 'data_count': sum(r['kind'] in DATA_RELATIONS for r in group['relations']),
                          'context_count': len(group['context_ids']), 'pending_relations': pending})
    callables = [e for e in graph['entities'] if e['kind'] in {'function', 'method'}]
    pending_relations = []
    for r in graph['relations']:
        if r['basis'] == 'source' and r.get('freshness', 'current') == 'current':
            continue
        def name(id):
            return entities.get(ports.get(id, {}).get('entity_id', id), {}).get('name', id)
        pending_relations.append({'id': r['id'], 'from': name(r['from_id']), 'to': name(r['to_id']), 'kind': r['kind'],
                                  'reason': r.get('reason') or ('依据需要重新核对' if r.get('freshness') == 'stale' else '关系尚未确认'),
                                  'context_id': r.get('context_id'), 'entity_id': ports.get(r['from_id'], {}).get('entity_id', r['from_id'])})
    files = [{'id': s['id'], 'path': s['path'], 'language': s['language'], 'category': s['category'],
              'read': s['read_state'] == 'read', 'included': s['included'],
              'reason': s.get('reason', '')} for s in graph['sources']]
    return {'reading': {k: summary[k] for k in ('status', 'sources_read', 'sources_total')},
            'file_count': len(files), 'read_file_count': sum(f['read'] and f['included'] for f in files), 'files': files,
            'scenarios': scenarios, 'scenario_count': len(scenarios), 'callable_count': len(callables),
            'callables_in_scenarios': sum(e['id'] in in_scenarios for e in callables),
            'outside_scenarios': [{'id': e['id'], 'name': e['name'], 'source_ids': e['source_ids'],
                                   'reviewed': e['analysis'] == 'reviewed' and e['freshness'] == 'current'} for e in callables if e['id'] not in in_scenarios],
            'pending_relations': pending_relations, 'pending_tasks': sum(t['state'] != 'done' and t['required'] for t in graph['tasks']),
            'scope_note': '文件阅读、场景整理和路径穷尽分别统计。未纳入场景的函数不一定未读；已有场景不代表覆盖所有分支和运行路径。'}
