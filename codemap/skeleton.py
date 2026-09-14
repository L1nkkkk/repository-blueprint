"""Durable syntax facts and bounded read-only queries; no model invocation."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import sqlite3
import subprocess
from pathlib import Path
import textwrap

from .core import canonical, require
from .repository import scan_repository, source_bytes, decode_text
from .syntax import backend
from .parser_runner import parse
from .change_analysis import cosmetic_signature, classify_change, exact_renames

SCHEMA = '''
CREATE TABLE IF NOT EXISTS skeleton_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
 language TEXT, parser_id TEXT, state TEXT, commit_id TEXT, updated_at TEXT, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id),
 kind TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL, parent_id TEXT,
 start_line INTEGER, end_line INTEGER, signature TEXT, body_sha TEXT NOT NULL, updated_at TEXT,
 document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_id);
CREATE TABLE IF NOT EXISTS edges (src_id TEXT NOT NULL, dst_id TEXT NOT NULL, kind TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('resolved_local','unique_in_project','ambiguous','unresolved')),
 site_line INTEGER NOT NULL, PRIMARY KEY(src_id,dst_id,kind,site_line));
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id,kind);
CREATE TABLE IF NOT EXISTS semantics (node_id TEXT NOT NULL REFERENCES nodes(id), body_sha TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('current','stale','suspect')), summary TEXT NOT NULL,
 detail TEXT NOT NULL, evidence TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(node_id,created_at));
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(node_id UNINDEXED,qualified_name,signature,summary);
CREATE TABLE IF NOT EXISTS semantic_tasks (node_id TEXT PRIMARY KEY REFERENCES nodes(id), body_sha TEXT NOT NULL,
 state TEXT NOT NULL, priority INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL,
 lease_id TEXT, worker TEXT, expires_at REAL, generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS semantic_receipts (batch_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
'''

@contextmanager
def connection(store, *, write=False):
    db = sqlite3.connect(store.path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
    finally:
        db.close()


def summary(store):
    with connection(store) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='nodes'").fetchone():
            return {'state':'not_initialized','next_action':'Run sync to create the persistent skeleton'}
        return {'state':'ready', **{t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('files','nodes','edges')},
                'file_states':{r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM files GROUP BY state')},
                'semantic_tasks':{r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM semantic_tasks GROUP BY state')},
                'budget_remaining':_meta(db,'budget_remaining',0)}


def initialize(store):
    with connection(store) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)


def _meta(db, key, default=None):
    row = db.execute('SELECT value FROM skeleton_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _save_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO skeleton_meta VALUES (?,?)', (key, canonical(value)))


def _semantic(db, node):
    row = db.execute('SELECT * FROM semantics WHERE node_id=? ORDER BY created_at DESC LIMIT 1', (node['id'],)).fetchone()
    if not row:
        return None
    row = dict(row)
    if row['body_sha'] != node['body_sha']:
        row['status'] = 'stale'
    row['detail'], row['evidence'] = json.loads(row['detail']), json.loads(row['evidence'])
    return row


def refresh_search(db):
    db.execute('DELETE FROM search')
    db.execute('''INSERT INTO search SELECT n.id,n.qualified_name,n.signature,
      coalesce((SELECT summary FROM semantics s WHERE s.node_id=n.id ORDER BY created_at DESC LIMIT 1),'')
      FROM nodes n ORDER BY n.id''')


def _resolve(db):
    nodes = [dict(r) for r in db.execute('SELECT * FROM nodes ORDER BY id')]
    by_name, local = defaultdict(list), defaultdict(list)
    for n in nodes:
        for key in {n['name'], n['qualified_name']}:
            by_name[key].append(n)
        local[(n['file_id'], n['qualified_name'])].append(n)
    db.execute('DELETE FROM edges')
    edges = set()
    for n in nodes:
        edges.add((n['parent_id'] or n['file_id'], n['id'], 'contains', 'resolved_local', n['start_line']))
    for f in db.execute('SELECT id,document FROM files ORDER BY id'):
        for s in json.loads(f['document'])['sites']:
            src = s['enclosing_symbol_id'] or f['id']
            name = s['text'].replace('::', '.').replace('->', '.')
            candidates, confidence = [], 'unresolved'
            if s['kind'] == 'call':
                candidates = local.get((f['id'], name), []) if '.' in name else []
                if '.' not in name:
                    # Lexical scopes are considered before project-wide names.
                    source = db.execute('SELECT qualified_name FROM nodes WHERE id=?', (src,)).fetchone()
                    scope = source[0].split('.') if source else []
                    while scope and not candidates:
                        candidates = local.get((f['id'], '.'.join(scope + [name])), [])
                        scope.pop()
                    candidates = candidates or local.get((f['id'], name), [])
                if candidates:
                    confidence = 'resolved_local' if len(candidates) == 1 else 'ambiguous'
                else:
                    candidates = by_name.get(name, [])
                    confidence = 'unique_in_project' if len(candidates) == 1 else 'ambiguous' if candidates else 'unresolved'
            for dst in [c['id'] for c in candidates] or ['name:' + s['text']]:
                edges.add((src, dst, s['kind'], confidence, s['start_line']))
    db.executemany('INSERT INTO edges VALUES (?,?,?,?,?)', sorted(edges))


def _rebind_cosmetic(db, before, after):
    # Preserve an auditable machine proof when byte/line changes are cosmetic.
    db.execute('DELETE FROM semantic_tasks WHERE node_id=?', (before['id'],))
    for r in db.execute('SELECT * FROM semantics WHERE node_id=? AND body_sha=?', (before['id'],before['body_sha'])).fetchall():
        evidence = json.loads(r['evidence'])
        for e in evidence:
            e.update(body_sha=after['body_sha'],start_line=after['start_line'],end_line=after['end_line'])
        detail = json.loads(r['detail'])
        detail['static_revalidation'] = {'basis':'equivalent_syntax','previous_body_sha':before['body_sha']}
        db.execute('UPDATE semantics SET body_sha=?,evidence=?,detail=? WHERE node_id=? AND created_at=?',
                   (after['body_sha'],canonical(evidence),canonical(detail),before['id'],r['created_at']))


def _parse_item(item):
    source, text = item
    result = parse(source, text)
    for n in result['symbols']:
        snippet = text.encode('utf-8')[n['start_byte']:n['end_byte']].decode('utf-8')
        normalized = cosmetic_signature(textwrap.dedent(snippet), source['language'])
        n.setdefault('semantic_sha', sha256(canonical(normalized).encode()).hexdigest() if normalized is not None else n['body_sha'])
        n['source_text'] = snippet
    return result


def sync(store, *, budget=0, langs=None, include=None, workers=4):
    require(type(budget) is int and budget >= 0, 'budget must be a nonnegative integer')
    require(type(workers) is int and 1 <= workers <= 16, 'workers must be 1..16')
    initialize(store)
    graph = store.read()
    root = Path(graph['project']['source_root'])
    fresh = scan_repository(root, excludes=graph['inventory']['exclusion_patterns'])
    require(not fresh['inventory']['gaps'], 'Resolve inventory gaps before skeleton sync')
    with connection(store) as db:
        old = {r['path']: dict(r) for r in db.execute('SELECT * FROM files')}
        previous_version = _meta(db, 'version', 0)
        config = _meta(db, 'config', {'langs': None, 'include': None})
    config = {'langs': langs if langs is not None else config['langs'], 'include': include if include is not None else config['include']}
    sources = {s['path']: s for s in fresh['sources'] if s['included'] and s['language']
               and (not config['langs'] or s['language'] in config['langs'])
               and (not config['include'] or any(fnmatchcase(s['path'], p) for p in config['include']))}
    try:
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip() or None
    except (OSError,subprocess.TimeoutExpired):
        commit = None
    # Hash reconciliation also covers dirty and untracked files, unlike HEAD-only diff.
    rename_before = {p: json.loads(r['document'])['source'] for p, r in old.items()}
    renames = exact_renames(rename_before, sources)
    renamed = {r['to']: r['from'] for r in renames}
    items, retained, unavailable = [], {}, {}
    file_ids, occupied = {}, {r['id'] for r in old.values()}
    for path, s in sorted(sources.items()):
        prior = old.get(path) or old.get(renamed.get(path))
        file_id = prior['id'] if prior else s['id']
        if not prior and file_id in occupied:
            # A relocated file can still own the ID originally derived from this path.
            salt = 0
            while file_id in occupied:
                file_id = 'source:' + sha256(canonical([s['id'],s['sha256'],salt]).encode()).hexdigest()[:24]
                salt += 1
        file_ids[path] = file_id
        occupied.add(file_id)
        engine = backend(s['language'], path)['id']
        if prior and prior['sha256'] == s['sha256'] and prior['parser_id'] == engine and prior['state'] != 'unavailable':
            row = json.loads(prior['document'])
            retained[path] = row
        elif not s.get('encoding'):
            unavailable[path] = {'source_id':s['id'],'path':path,'sha256':s['sha256'],'language':s['language'],
                                 'parser_id':engine,'state':'unavailable','symbols':[],'sites':[],
                                 'diagnostics':[{'message':'Source is binary, oversized or has unsupported encoding','line':None}],
                                 'diagnostic_count':1}
        else:
            _, raw = source_bytes(fresh, root, s['id'])
            items.append((dict(s,id=file_id), decode_text(raw)[0]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        parsed = list(pool.map(_parse_item, items))
    results = {r['path']: r for r in parsed} | retained | unavailable
    if not parsed and not unavailable and set(retained)==set(old) and not renames:
        with connection(store,write=True) as db:
            require(_meta(db,'version',0)==previous_version,'Concurrent skeleton sync; retry')
            db.execute('UPDATE files SET commit_id=?',(commit,))
            refresh_search(db)
            _save_meta(db,'budget_remaining',budget)
            _save_meta(db,'config',config)
            _save_meta(db,'version',previous_version+1)
            counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('files','nodes','edges','semantic_tasks')}
        return {**counts,'parsed_files':0,'cache_hits':len(retained),'unavailable_files':0,
                'changed':[],'deleted':[],'suspect':[],'renames':[],'budget_remaining':budget,'llm_tokens':0}
    changed, suspect, deleted = set(), set(), set()
    with connection(store, write=True) as db:
        require(_meta(db, 'version', 0) == previous_version, 'Concurrent skeleton sync; retry')
        old_nodes = {r['id']: json.loads(r['document']) for r in db.execute('SELECT id,document FROM nodes')}
        old_edges = [dict(r) for r in db.execute("SELECT * FROM edges WHERE kind='call'")]
        new_nodes, contracts = {}, set()
        for path, row in sorted(results.items()):
            s = sources[path]
            prior = old.get(path) or old.get(renamed.get(path))
            file_id = file_ids[path]
            # Preserve moved or cosmetic-equivalent declarations using a unique verified match.
            if prior and path not in retained:
                idmap = {}
                previous = json.loads(prior['document'])
                lookup = defaultdict(list)
                for n in previous['symbols']:
                    lookup[(n['kind'], n['qualified_name'], n['signature'])].append(n['id'])
                equivalent = defaultdict(list)
                for n in previous['symbols']:
                    equivalent[(n['kind'],n['qualified_name'],n.get('semantic_sha'))].append(n['id'])
                for n in row['symbols']:
                    matches = lookup[(n['kind'], n['qualified_name'], n['signature'])]
                    if matches:
                        idmap[n['id']] = matches.pop(0)
                    else:
                        exact = equivalent[(n['kind'],n['qualified_name'],n.get('semantic_sha'))]
                        if len(exact)==1:
                            idmap[n['id']] = exact[0]
                for n in row['symbols']:
                    n['id'] = idmap.get(n['id'], n['id'])
                    n['parent_id'] = idmap.get(n['parent_id'], n['parent_id'])
                for site in row['sites']:
                    site['enclosing_symbol_id'] = idmap.get(site['enclosing_symbol_id'], site['enclosing_symbol_id'])
            row.update(source_id=file_id, path=path, source=s)
            classification = None
            if prior and path not in retained and path not in unavailable:
                before = json.loads(prior['document'])
                classification = classify_change(graph, s, before.get('text'), next(t for x,t in items if x['path']==path))['kind']
                before_imports = [x['text'] for x in before['sites'] if x['kind']=='import']
                after_imports = [x['text'] for x in row['sites'] if x['kind']=='import']
                if before_imports != after_imports and classification != 'cosmetic':
                    contracts.update(n['id'] for n in before['symbols'])
                    changed.update(n['id'] for n in row['symbols'])
            row['text'] = next((t for x,t in items if x['path']==path), row.get('text', ''))
            stamp = s['sha256'] + ':' + row['parser_id']
            db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
             path=excluded.path,sha256=excluded.sha256,language=excluded.language,parser_id=excluded.parser_id,
             state=excluded.state,commit_id=excluded.commit_id,updated_at=excluded.updated_at,document=excluded.document''',
             (file_id,path,s['sha256'],s['language'],row['parser_id'],row['state'],commit,stamp,canonical(row)))
            for n in row['symbols']:
                n = dict(n, file_id=file_id)
                new_nodes[n['id']] = n
                prev = old_nodes.get(n['id'])
                if prev and prev['body_sha'] != n['body_sha']:
                    if prev.get('semantic_sha') == n.get('semantic_sha') or classification == 'cosmetic':
                        _rebind_cosmetic(db, prev, n)
                    else:
                        changed.add(n['id'])
                        if classification == 'contract':
                            contracts.add(n['id'])
                elif not prev:
                    changed.add(n['id'])
                elif prev['start_line'] != n['start_line']:
                    _rebind_cosmetic(db, prev, n)
                db.execute('''INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                 file_id=excluded.file_id,kind=excluded.kind,name=excluded.name,qualified_name=excluded.qualified_name,
                 parent_id=excluded.parent_id,start_line=excluded.start_line,end_line=excluded.end_line,
                 signature=excluded.signature,body_sha=excluded.body_sha,updated_at=excluded.updated_at,document=excluded.document''',
                 (n['id'],file_id,n['kind'],n['name'],n['qualified_name'],n['parent_id'],n['start_line'],n['end_line'],n['signature'],n['body_sha'],n['body_sha'],canonical(n)))
        deleted = old_nodes.keys() - new_nodes.keys()
        contracts.update(deleted)
        suspect = {e['src_id'] for e in old_edges if e['dst_id'] in contracts} & new_nodes.keys() - changed
        for id in changed:
            db.execute("UPDATE semantics SET status='stale' WHERE node_id=?", (id,))
            db.execute('DELETE FROM semantic_tasks WHERE node_id=?', (id,))
        for id in suspect:
            db.execute("UPDATE semantics SET status='suspect' WHERE node_id=? AND status='current'", (id,))
            db.execute('DELETE FROM semantic_tasks WHERE node_id=?', (id,))
        for id in deleted:
            db.execute('DELETE FROM semantics WHERE node_id=?', (id,))
            db.execute('DELETE FROM semantic_tasks WHERE node_id=?', (id,))
            db.execute('DELETE FROM nodes WHERE id=?', (id,))
        active = {n['file_id'] for n in new_nodes.values()} | {r['source_id'] for r in results.values()}
        for r in db.execute('SELECT id FROM files').fetchall():
            if r['id'] not in active:
                db.execute('DELETE FROM files WHERE id=?', (r['id'],))
        _resolve(db)
        before_targets, after_targets = defaultdict(set), defaultdict(set)
        for e in old_edges:
            before_targets[e['src_id']].add((e['dst_id'],e['confidence']))
        for e in db.execute("SELECT * FROM edges WHERE kind='call'"):
            after_targets[e['src_id']].add((e['dst_id'],e['confidence']))
        resolution_changed = {id for id in old_nodes.keys() & new_nodes.keys() - changed
                              if before_targets[id] != after_targets[id]}
        for id in resolution_changed - suspect:
            db.execute("UPDATE semantics SET status='suspect' WHERE node_id=? AND status='current'", (id,))
            db.execute('DELETE FROM semantic_tasks WHERE node_id=?', (id,))
        suspect.update(resolution_changed)
        for n in new_nodes.values():
            sem = _semantic(db,n)
            if sem and sem['status'] == 'current':
                continue
            degree = db.execute("SELECT count(*) FROM edges WHERE dst_id=? AND kind='call'", (n['id'],)).fetchone()[0]
            estimate = max(128, (len(n.get('source_text','').encode('utf-8')) + 2)//3 + 512)
            db.execute('INSERT OR IGNORE INTO semantic_tasks(node_id,body_sha,state,priority,estimated_tokens) VALUES (?,?,?,?,?)',
                       (n['id'],n['body_sha'],'queued',degree*10 - (1 if sem and sem['status']=='suspect' else 0),estimate))
        refresh_search(db)
        _save_meta(db,'config',config)
        _save_meta(db,'root',str(root))
        _save_meta(db,'budget_remaining',budget)
        _save_meta(db,'version',previous_version+1)
        counts = {t: db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('files','nodes','edges','semantic_tasks')}
    from .structure import cache_key
    for path, row in results.items():
        if row['source_id'] == sources[path]['id']:
            key = cache_key(sources[path])
            store.save_structure(key, dict(row, index_id=key))
    return {**counts,'unavailable_files':len(unavailable),'parsed_files':len(parsed),'cache_hits':len(retained),'changed':sorted(changed),
            'deleted':sorted(deleted),'suspect':sorted(suspect),'renames':renames,'budget_remaining':budget,'llm_tokens':0}


def _record(db, row):
    node = dict(row)
    node.pop('document',None)
    node['semantics'] = _semantic(db,node)
    return node


def find_symbol(store, name, fuzzy=False, kind=None, limit=30):
    require(type(limit) is int and 1 <= limit <= 500, 'limit must be 1..500')
    sql = 'SELECT n.*,f.path FROM nodes n JOIN files f ON n.file_id=f.id WHERE '
    if fuzzy:
        sql += '(instr(lower(n.name),lower(?))>0 OR instr(lower(n.qualified_name),lower(?))>0)'
        values = [name,name]
    else:
        sql += '(n.id=? OR n.name=? OR n.qualified_name=?)'
        values = [name,name,name]
    if kind:
        sql += ' AND n.kind=?'
        values.append(kind)
    with connection(store) as db:
        rows = db.execute(sql+' ORDER BY f.path,n.start_line,n.id LIMIT ?',values+[limit+1]).fetchall()
        return {'symbols':[_record(db,r) for r in rows[:limit]],'truncated':len(rows)>limit}


def calls(store, symbol, *, direction='callers', depth=1, limit=100):
    require(type(depth) is int and 1 <= depth <= 3, 'depth must be 1..3')
    require(type(limit) is int and 1 <= limit <= 500, 'limit must be 1..500')
    roots = find_symbol(store,symbol,limit=500)
    require(len(roots['symbols']) == 1, 'Use an exact symbol ID: name is missing or ambiguous')
    root = roots['symbols'][0]['id']
    seen, frontier, edges = {root}, {root}, []
    field, other = ('dst_id','src_id') if direction=='callers' else ('src_id','dst_id')
    truncated = False
    with connection(store) as db:
        for level in range(1,depth+1):
            following = set()
            for id in sorted(frontier):
                rows = db.execute("SELECT * FROM edges WHERE kind='call' AND "+field+'=? ORDER BY src_id,dst_id,site_line LIMIT ?', (id,limit-len(edges)+1)).fetchall()
                for row in rows:
                    if len(edges)>=limit:
                        truncated = True
                        break
                    edge = dict(row,depth=level)
                    edges.append(edge)
                    if row[other] not in seen:
                        following.add(row[other])
                if truncated: break
            seen.update(following)
            frontier = following
            if truncated or not frontier: break
        nodes = []
        for id in sorted(seen):
            row = db.execute('SELECT n.*,f.path FROM nodes n JOIN files f ON n.file_id=f.id WHERE n.id=?',(id,)).fetchone()
            if row: nodes.append(_record(db,row))
    return {'root':root,'edges':edges,'symbols':nodes,'truncated':truncated}


def search(store, query, limit=30):
    require(type(limit) is int and 1 <= limit <= 500, 'limit must be 1..500')
    terms = query.split()
    require(bool(terms),'query cannot be empty')
    match = ' AND '.join('"'+t.replace('"','""')+'"' for t in terms)
    with connection(store) as db:
        rows = db.execute('''SELECT n.*,f.path,bm25(search) AS rank FROM search
         JOIN nodes n ON n.id=search.node_id JOIN files f ON f.id=n.file_id
         WHERE search MATCH ? ORDER BY rank,n.id LIMIT ?''',(match,limit+1)).fetchall()
        return {'symbols':[_record(db,r) for r in rows[:limit]],'truncated':len(rows)>limit}


def repo_map(store, path='', budget_tokens=2000):
    require(type(budget_tokens) is int and 0 <= budget_tokens <= 32000, 'budget_tokens must be 0..32000')
    lines, used = [], 0
    with connection(store) as db:
        rows = db.execute('''SELECT n.*,f.path,(SELECT count(*) FROM edges e WHERE e.dst_id=n.id AND e.kind='call') degree
         FROM nodes n JOIN files f ON f.id=n.file_id WHERE substr(f.path,1,?)=? ORDER BY degree DESC,f.path,n.start_line,n.id''',(len(path),path))
        truncated = False
        for r in rows:
            line = f"{r['path']}:{r['start_line']}  {r['signature']}"
            # UTF-8 byte count is a conservative upper bound, independent of tokenizer.
            cost = len((line+'\n').encode('utf-8'))
            if used+cost > budget_tokens:
                truncated=True
                continue
            lines.append(line)
            used += cost
    return {'text':'\n'.join(lines),'budget_tokens':budget_tokens,'token_upper_bound':used,'truncated':truncated}


def doctor(store):
    with connection(store) as db:
        errors = [str(tuple(r)) for r in db.execute('PRAGMA foreign_key_check')]
        errors += [r[0] for r in db.execute('PRAGMA integrity_check') if r[0]!='ok']
        bad = db.execute("SELECT count(*) FROM semantics s JOIN nodes n ON n.id=s.node_id WHERE s.status!='stale' AND s.body_sha!=n.body_sha").fetchone()[0]
        if bad: errors.append(f'{bad} unmarked semantic hash mismatches')
        dangling = db.execute('''SELECT count(*) FROM edges e WHERE
          (NOT EXISTS(SELECT 1 FROM nodes n WHERE n.id=e.src_id) AND NOT EXISTS(SELECT 1 FROM files f WHERE f.id=e.src_id))
          OR (e.dst_id NOT LIKE 'name:%' AND NOT EXISTS(SELECT 1 FROM nodes n WHERE n.id=e.dst_id))''').fetchone()[0]
        if dangling: errors.append(f'{dangling} dangling edges')
        bad_tasks = db.execute('SELECT count(*) FROM semantic_tasks t JOIN nodes n ON t.node_id=n.id WHERE t.body_sha!=n.body_sha').fetchone()[0]
        if bad_tasks: errors.append(f'{bad_tasks} mismatched task hashes')
        expected = [(r['id'],r['qualified_name'],r['signature'],(_semantic(db,r) or {}).get('summary','')) for r in db.execute('SELECT * FROM nodes ORDER BY id')]
        actual = [tuple(r) for r in db.execute('SELECT * FROM search ORDER BY node_id')]
        if expected != actual: errors.append('FTS index differs from symbols/semantics')
        return {'ok':not errors,'errors':errors,'nodes':len(expected)}
