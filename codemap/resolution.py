"""Persistent syntax-site lookup and subset edge resolution without per-site node queries."""
from collections import defaultdict

SITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS sites (
 file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL,
 src_id TEXT NOT NULL, kind TEXT NOT NULL, target_name TEXT NOT NULL, text TEXT NOT NULL,
 site_line INTEGER NOT NULL, end_line INTEGER NOT NULL, text_truncated INTEGER NOT NULL, PRIMARY KEY(file_id,ordinal));
CREATE INDEX IF NOT EXISTS idx_sites_target ON sites(target_name);
CREATE INDEX IF NOT EXISTS idx_sites_origin ON sites(src_id,kind,site_line);
'''


def save_sites(db, file_id, sites):
    db.execute('DELETE FROM sites WHERE file_id=?', (file_id,))
    db.executemany('INSERT INTO sites VALUES (?,?,?,?,?,?,?,?,?)',
                   [(file_id,i,s['enclosing_symbol_id'] or file_id,s['kind'],
                     s['text'].replace('::','.').replace('->','.'),s['text'],s['start_line'],s['end_line'],int(s.get('text_truncated',False)))
                    for i,s in enumerate(sites)])


def edge_rows(nodes, sites):
    by_id = {n['id']:n for n in nodes}
    by_name, local = defaultdict(list), defaultdict(list)
    for n in nodes:
        for key in {n['name'],n['qualified_name']}:
            by_name[key].append(n)
        local[(n['file_id'],n['qualified_name'])].append(n)
    edges = set()
    for s in sites:
        src, name = s['src_id'],s['target_name']
        candidates, confidence = [],'unresolved'
        if s['kind']=='call':
            candidates = local.get((s['file_id'],name),[]) if '.' in name else []
            if '.' not in name:
                scope = by_id[src]['qualified_name'].split('.') if src in by_id else []
                while scope and not candidates:
                    candidates = local.get((s['file_id'],'.'.join(scope+[name])),[])
                    scope.pop()
                candidates = candidates or local.get((s['file_id'],name),[])
            if candidates:
                confidence = 'resolved_local' if len(candidates)==1 else 'ambiguous'
            else:
                candidates = by_name.get(name,[])
                confidence = 'unique_in_project' if len(candidates)==1 else 'ambiguous' if candidates else 'unresolved'
        for dst in [c['id'] for c in candidates] or ['name:'+s['text']]:
            edges.add((src,dst,s['kind'],confidence,s['site_line']))
    return edges


def _call_snapshot(db, origins):
    ids, targets, destinations = list(origins),defaultdict(set),set()
    for offset in range(0,len(ids),400):
        page = ids[offset:offset+400]
        for r in db.execute("SELECT src_id,dst_id,confidence FROM edges WHERE kind='call' AND src_id IN ("+','.join('?' for _ in page)+')',page):
            targets[r['src_id']].add((r['dst_id'],r['confidence']))
            destinations.add(r['dst_id'])
    return targets,destinations


def resolve(db, *, file_ids=None, names=(), removed_sources=()):
    """None means full reference rebuild; routine sync always supplies affected files."""
    nodes = [dict(r) for r in db.execute('SELECT id,file_id,kind,name,qualified_name,parent_id,start_line FROM nodes')]
    if file_ids is None:
        selected = db.execute('SELECT * FROM sites').fetchall()
        affected = {r[0] for r in db.execute('SELECT DISTINCT src_id FROM edges')} | {r['src_id'] for r in selected}
        before,old_destinations = _call_snapshot(db,affected)
        db.execute('DELETE FROM edges')
        containers = nodes
    else:
        file_ids = set(file_ids)
        db.execute('CREATE TEMP TABLE IF NOT EXISTS affected_files (id TEXT PRIMARY KEY)')
        db.execute('CREATE TEMP TABLE IF NOT EXISTS affected_names (name TEXT PRIMARY KEY)')
        db.execute('CREATE TEMP TABLE IF NOT EXISTS affected_sites (src_id TEXT,kind TEXT,site_line INTEGER, PRIMARY KEY(src_id,kind,site_line))')
        for table in ('affected_files','affected_names','affected_sites'):
            db.execute('DELETE FROM '+table)
        db.executemany('INSERT INTO affected_files VALUES (?)', [(id,) for id in sorted(file_ids)])
        db.executemany('INSERT INTO affected_names VALUES (?)', [(name,) for name in sorted(set(names))])
        db.execute('''INSERT OR IGNORE INTO affected_sites SELECT src_id,kind,site_line FROM sites
                      WHERE file_id IN (SELECT id FROM affected_files)''')
        db.execute('''INSERT OR IGNORE INTO affected_sites SELECT src_id,kind,site_line FROM sites
                      WHERE target_name IN (SELECT name FROM affected_names)''')
        # Recompute all sites sharing a collapsed edge key, including untouched targets on the same line.
        selected = db.execute('''SELECT s.* FROM affected_sites a JOIN sites s
                              ON s.src_id=a.src_id AND s.kind=a.kind AND s.site_line=a.site_line''').fetchall()
        origins = set(removed_sources) | file_ids | {n['id'] for n in nodes if n['file_id'] in file_ids}
        affected = origins | {r['src_id'] for r in selected}
        before,old_destinations = _call_snapshot(db,affected)
        db.executemany('DELETE FROM edges WHERE src_id=? AND kind=? AND site_line=?',
                       [tuple(r) for r in db.execute('SELECT * FROM affected_sites')])
        # Changed files may have removed sites whose keys no longer exist in the site index.
        db.executemany('DELETE FROM edges WHERE src_id=?', [(id,) for id in sorted(origins)])
        containers = [n for n in nodes if n['file_id'] in file_ids]
    edges = edge_rows(nodes,selected)
    edges.update((n['parent_id'] or n['file_id'],n['id'],'contains','resolved_local',n['start_line']) for n in containers)
    db.executemany('INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)', sorted(edges))
    after,new_destinations = _call_snapshot(db,affected)
    return {'resolved_sites':len(selected),'written_edges':len(edges),
            'resolution_changed':{id for id in affected if before[id]!=after[id]},
            'degree_changed':old_destinations|new_destinations}
