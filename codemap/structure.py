"""Hash-keyed local structure cache, canvas projection and concise Agent reuse."""

from collections import Counter, defaultdict
from copy import deepcopy
from time import perf_counter

from .core import digest, require
from .repository import source_record, source_bytes, decode_text
from .syntax import backend
from .parser_runner import parse


def cache_key(source):
    return 'index:' + digest([source['id'], source['path'], source['sha256'], backend(source['language'], source['path'])['id']])


def candidates(graph):
    return [s for s in graph['sources'] if s['included'] and s['language']]


def cached(store, graph):
    keys = {cache_key(s): s for s in candidates(graph)}
    return {keys[key]['id']: value for key, value in store.cached_structure(keys).items()}


def file_summary(row):
    return {key: row[key] for key in ('source_id', 'path', 'sha256', 'parser_id', 'index_id', 'state')} | {
        'symbol_count': len(row['symbols']), 'site_count': len(row['sites']),
        'diagnostic_count': row['diagnostic_count'], 'diagnostics': row['diagnostics'][:5]}


def build_index(store, *, source=None, offset=0, limit=100, snapshot_id=None):
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 500, 'invalid index page')
    started = perf_counter()
    graph = store.read()
    require(snapshot_id in (None, graph['project']['snapshot_id']), 'Source snapshot changed; restart indexing at offset 0.')
    selected = [source_record(graph, source)] if source is not None else candidates(graph)
    page = selected[offset:offset + limit]
    stored = store.cached_structure(cache_key(s) for s in page)
    files, parsed, reused = [], 0, 0
    for s in page:
        if files and perf_counter() - started >= 8:
            break
        key = cache_key(s)
        try:
            _, raw = source_bytes(graph, graph['project']['source_root'], s['id'])
            if key in stored:
                row = stored[key]
                reused += 1
            else:
                row = parse(s, decode_text(raw)[0])
                row['index_id'] = key
                if row['state'] != 'unavailable':
                    store.save_structure(key, row)
                parsed += 1
            files.append(file_summary(row))
        except (OSError, ValueError) as error:
            files.append({'source_id': s['id'], 'path': s['path'], 'state': 'unavailable',
                          'symbol_count': 0, 'site_count': 0, 'diagnostic_count': 1,
                          'diagnostics': [{'message': str(error)}]})
    return {'snapshot_id': graph['project']['snapshot_id'], 'total_files': len(selected),
            'files': files, 'parsed_files': parsed, 'cache_hits': reused,
            'elapsed_ms': round((perf_counter() - started) * 1000, 2),
            'next_offset': offset + len(files) if offset + len(files) < len(selected) else None,
            'analysis_progress_changed': False, 'ai_started': False}


def index_status(store, graph=None, *, unavailable_sources=()):
    graph = graph or store.read()
    rows = cached(store, graph)
    states = Counter('source_changed' if s['id'] in unavailable_sources else rows[s['id']]['state'] if s['id'] in rows else 'pending'
                     for s in candidates(graph))
    current = [r for id, r in rows.items() if id not in unavailable_sources]
    return {'files_total': len(candidates(graph)), 'states': dict(states),
            'symbols': sum(len(r['symbols']) for r in current),
            'syntax_sites': sum(len(r['sites']) for r in current),
            'semantics_reviewed': False, 'version': digest([(r['index_id'], r['state']) for r in rows.values()])}


def query_index(store, *, source, table='symbols', offset=0, limit=30, index_id=None):
    require(table in {'symbols', 'sites', 'diagnostics'}, 'unknown structural table')
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, 'invalid structure page')
    graph = store.read()
    s, _ = source_bytes(graph, graph['project']['source_root'], source)
    key = cache_key(s)
    require(index_id in (None, key), 'Index changed; restart structural pagination at offset 0.')
    row = store.cached_structure([key]).get(key)
    require(row is not None, 'No current index for this file; call blueprint_index(action="build", source=...) first.')
    records = row[table]
    return {**file_summary(row), 'table': table, 'records': records[offset:offset + limit], 'total': len(records),
            'next_offset': offset + limit if offset + limit < len(records) else None,
            'limitations': row['limitations'], 'read_progress_changed': False}


def task_index(store, source_ids):
    started = perf_counter()
    graph = store.read()
    sources = [s for s in graph['sources'] if s['id'] in source_ids and s['included'] and s['language']]
    summaries = []
    for s in sources[:20]:
        if summaries and perf_counter() - started >= 8:
            break
        result = build_index(store, source=s['id'])
        item = result['files'][0]
        if item['state'] in {'parsed', 'partial'}:
            page = query_index(store, source=s['id'], limit=15)
            item = {**item, 'symbols': [{k: r[k] for k in ('id', 'kind', 'qualified_name', 'start_line', 'end_line')}
                                      for r in page['records']], 'next_offset': page['next_offset']}
        summaries.append(item)
    return {'files': summaries, 'file_count': len(sources),
            'use': 'Page blueprint_index(action="query", source=..., index_id=...). Read actual source. In prepare use symbol_id for entities or evidence; structure, parents and source ranges are filled automatically. Syntax sites are unresolved, never established data flows.'}


def _matches(graph, rows):
    """Match legacy entities only with an unambiguous name/kind AND source range."""
    matches, reverse = {}, defaultdict(list)
    entities = {e['id']: e for e in graph['entities']}
    proofs = {e['id']: e for e in graph['evidence']}
    by_source = defaultdict(list)
    for e in entities.values():
        for id in e['source_ids']:
            by_source[id].append(e)
    for source_id, row in rows.items():
        for symbol in row['symbols']:
            if symbol['id'] in entities:
                matches[symbol['id']] = symbol['id']
                continue
            names = {symbol['name'].replace('::', '.'), symbol['qualified_name'].replace('::', '.')}
            choices = [e['id'] for e in by_source[source_id] if e['kind'] == symbol['kind'] and e['name'].replace('::', '.') in names and
                       any(p['source_id'] == source_id and p['sha256'] == row['sha256'] and p.get('freshness', 'current') == 'current'
                           and p['start_line'] <= symbol['start_line'] <= p['end_line'] <= symbol['end_line']
                           for id in e['evidence_ids'] if (p := proofs.get(id)))]
            if len(choices) == 1:
                matches[symbol['id']] = choices[0]
                reverse[choices[0]].append(symbol['id'])
    for ids in reverse.values():
        if len(ids) > 1:
            for id in ids:
                matches.pop(id, None)
    return matches


def project_structure(graph, rows):
    """Presentation only: never modifies the persisted graph or its completion report."""
    graph = deepcopy(graph)
    sources = {s['id']: s for s in graph['sources']}
    for entity in graph['entities']:
        anchor = entity.get('structure')
        if anchor and sources.get(anchor['source_id'], {}).get('sha256') != anchor['sha256']:
            entity.pop('structure')
    mapping = _matches(graph, rows)
    entities = {e['id']: e for e in graph['entities']}
    files = {id: e for e in graph['entities'] if e['kind'] == 'file' for id in e['source_ids']}
    links = {(m['parent_id'], m['child_id'], m['axis']) for m in graph['memberships']}
    for source_id, row in rows.items():
        if row['state'] not in {'parsed', 'partial'} or source_id not in files:
            continue
        for s in row['symbols']:
            id = mapping.get(s['id'], s['id'])
            if id not in entities:
                entities[id] = {'id': id, 'kind': s['kind'], 'name': s['name'], 'language': row['language'],
                                'roles': [], 'source_ids': [source_id], 'analysis': 'located', 'freshness': 'current',
                                'summary': '', 'evidence_ids': []}
                graph['entities'].append(entities[id])
            entities[id]['structure'] = {**s, 'index_id': row['index_id'], 'source_id': source_id,
                                          'sha256': row['sha256'], 'parser_id': row['parser_id'], 'state': row['state']}
            parent = mapping.get(s['parent_id'], s['parent_id']) if s['parent_id'] else files[source_id]['id']
            link = (parent, id, 'semantic')
            if parent != id and link not in links:
                graph['memberships'].append({'id': 'structure-member:' + digest(link)[:24],
                                             'parent_id': parent, 'child_id': id, 'axis': 'semantic'})
                links.add(link)
    return graph


def selected_entity(store, graph, id):
    saved = next((e for e in graph['entities'] if e['id'] == id), None)
    if saved is not None:
        return saved
    row, symbol = lookup_symbol(store, graph, id)
    return next(e for e in project_structure(graph, {row['source_id']: row})['entities']
                if e.get('structure', {}).get('id') == symbol['id'])


def lookup_symbol(store, graph, symbol_id):
    rows = cached(store, graph)
    found = [(row, s) for row in rows.values() for s in row['symbols'] if s['id'] == symbol_id]
    require(len(found) == 1, 'Unknown or outdated symbol_id; query the current structural index.')
    row, symbol = found[0]
    source_bytes(graph, graph['project']['source_root'], row['source_id'])
    return row, symbol


def expand_findings(store, graph, upserts):
    """Fill structural boilerplate only. ReadLedger still checks every new claim."""
    groups = deepcopy(upserts)
    selected = {r['symbol_id'] for table in ('entities', 'evidence') for r in groups.get(table, []) if 'symbol_id' in r}
    if not selected:
        return groups, {}
    rows = {}
    for id in selected:
        row, _ = lookup_symbol(store, graph, id)
        rows[row['source_id']] = row
    projected = project_structure(graph, rows)
    mapping = _matches(graph, rows)
    entities = {e['id']: e for e in projected['entities']}
    stored = {e['id']: e for e in graph['entities']}
    symbols = {s['id']: (r, s) for r in rows.values() for s in r['symbols']}
    imports = {}
    for change in groups.get('entities', []):
        if 'symbol_id' not in change:
            continue
        symbol_id = change.pop('symbol_id')
        id = mapping.get(symbol_id, symbol_id)
        require('id' not in change or change['id'] == id, 'symbol_id and entity id disagree')
        change['id'] = id
        cursor = symbol_id
        while cursor:
            _, s = symbols[cursor]
            entity_id = mapping.get(cursor, cursor)
            imports[entity_id] = entities[entity_id]
            cursor = s['parent_id']
    for change in groups.get('evidence', []):
        if 'symbol_id' in change:
            row, s = symbols[change.pop('symbol_id')]
            require('source' not in change or change['source'] in {row['source_id'], row['path']}, 'symbol evidence source mismatch')
            change['source'] = row['source_id']
            change.setdefault('start_line', s['start_line'])
            change.setdefault('end_line', s['end_line'])
    explicit = {r.get('id') for r in groups.get('entities', [])}
    for id in imports:
        if id not in stored and id not in explicit:
            groups.setdefault('entities', []).append({'id': id})
    old_links = {m['id'] for m in graph['memberships']}
    members = [m for m in projected['memberships'] if m['child_id'] in imports and m['id'] not in old_links]
    # Virtual defaults are accepted only for the verified symbols requested above.
    defaults = {'entities': imports, 'memberships': {m['id']: m for m in members}}
    groups.setdefault('memberships', []).extend({'id': m['id']} for m in members)
    return groups, defaults
