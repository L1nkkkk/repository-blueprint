"""Four testable sync stages: reconcile, rebind, invalidate and enqueue."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from .core import canonical, require
from .repository import scan_repository, source_bytes, decode_text
from .syntax import backend
from .change_analysis import exact_renames
from .resolution import save_sites, resolve


def reconcile(store, *, budget=0, langs=None, include=None, workers=4, legacy_cache=None):
    """Read one skeleton generation, inventory hashes and persisted syntax metadata."""
    from . import skeleton as sk
    require(type(budget) is int and budget >= 0, 'budget must be a nonnegative integer')
    require(type(workers) is int and 1 <= workers <= 16, 'workers must be 1..16')
    graph = store.read()
    root = Path(graph['project']['source_root'])
    fresh = scan_repository(root, excludes=graph['inventory']['exclusion_patterns'])
    require(not fresh['inventory']['gaps'], 'Resolve inventory gaps before skeleton sync')
    with sk.connection(store) as db:
        db.execute('BEGIN')
        old = {r['path']:dict(r) for r in db.execute('SELECT * FROM files')}
        documents = sk.file_documents(db,old.values())
        version = sk._meta(db,'version',0)
        config = sk._meta(db,'config',{'langs':None,'include':None,'legacy_cache':False})
    config = {'langs':langs if langs is not None else config['langs'],
              'include':include if include is not None else config['include'],
              'legacy_cache':legacy_cache if legacy_cache is not None else config.get('legacy_cache',False)}
    sources = {s['path']:s for s in fresh['sources'] if s['included'] and s['language']
               and (not config['langs'] or s['language'] in config['langs'])
               and (not config['include'] or any(fnmatchcase(s['path'],p) for p in config['include']))}
    try:
        commit = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True,
                                timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout.strip() or None
    except (OSError,subprocess.TimeoutExpired):
        commit = None
    return dict(root=root,fresh=fresh,old=old,documents=documents,version=version,config=config,
                sources=sources,commit=commit,budget=budget,workers=workers)


def rebind(plan):
    """Keep historical identities, parse changed files, and compute structural deltas without writes."""
    from . import skeleton as sk
    old, sources, documents = plan['old'],plan['sources'],plan['documents']
    renames = exact_renames({p:json.loads(r['document'])['source'] for p,r in old.items()},sources)
    renamed = {r['to']:r['from'] for r in renames}
    file_ids, occupied = {},{r['id'] for r in old.values()}
    items, results, retained, unavailable = [],{},set(),set()
    for path,s in sorted(sources.items()):
        prior = old.get(path) or old.get(renamed.get(path))
        file_id = prior['id'] if prior else s['id']
        if not prior and file_id in occupied:
            salt = 0
            while file_id in occupied:
                file_id = 'source:'+sha256(canonical([s['id'],s['sha256'],salt]).encode()).hexdigest()[:24]
                salt += 1
        file_ids[path] = file_id
        occupied.add(file_id)
        engine = backend(s['language'],path)['id']
        if prior and prior['sha256']==s['sha256'] and prior['parser_id']==engine and prior['state']!='unavailable':
            # Avoid changing the captured baseline while remapping paths or callers.
            results[path] = dict(documents[prior['id']])
            retained.add(path)
        elif not s.get('encoding'):
            results[path] = dict(source_id=file_id,path=path,sha256=s['sha256'],language=s['language'],
                                 parser_id=engine,state='unavailable',symbols=[],sites=[],diagnostic_count=1,
                                 diagnostics=[{'message':'Source is binary, oversized or has unsupported encoding','line':None}])
            unavailable.add(path)
        else:
            _,raw = source_bytes(plan['fresh'],plan['root'],s['id'])
            items.append((dict(s,id=file_id),decode_text(raw)[0]))
    with ThreadPoolExecutor(max_workers=plan['workers']) as pool:
        parsed = list(pool.map(sk._parse_item,items))
    results.update({r['path']:r for r in parsed})
    old_nodes = {n['id']:dict(n,file_id=id) for id,row in documents.items() for n in row['symbols']}
    nodes,changed,contracts,fts_changed,cosmetic,affected_files = {},set(),set(),set(),[],set()
    for path,row in sorted(results.items()):
        source, file_id = sources[path],file_ids[path]
        prior = old.get(path) or old.get(renamed.get(path))
        before = documents[prior['id']] if prior else None
        if prior and path not in retained:
            lookup,equivalent,idmap = defaultdict(list),defaultdict(list),{}
            for n in before['symbols']:
                lookup[(n['kind'],n['qualified_name'],n['signature'])].append(n['id'])
                equivalent[(n['kind'],n['qualified_name'],n.get('semantic_sha'))].append(n['id'])
            for n in row['symbols']:
                matches = lookup[(n['kind'],n['qualified_name'],n['signature'])]
                exact = equivalent[(n['kind'],n['qualified_name'],n.get('semantic_sha'))]
                if matches:
                    idmap[n['id']] = matches.pop(0)
                elif len(exact)==1:
                    idmap[n['id']] = exact[0]
            for n in row['symbols']:
                n['id'] = idmap.get(n['id'],n['id'])
                n['parent_id'] = idmap.get(n['parent_id'],n['parent_id'])
            for site in row['sites']:
                site['enclosing_symbol_id'] = idmap.get(site['enclosing_symbol_id'],site['enclosing_symbol_id'])
        row.update(source_id=file_id,path=path,source=source)
        classification = None
        if path not in retained or path in renamed:
            affected_files.add(file_id)
        if prior and path not in retained and path not in unavailable:
            classification = ('cosmetic' if before.get('syntax_sha')==row.get('syntax_sha') and row.get('syntax_sha')
                              else 'contract' if before.get('contract_sha')!=row.get('contract_sha') else 'implementation')
            if ([x['text'] for x in before['sites'] if x['kind']=='import'] !=
                [x['text'] for x in row['sites'] if x['kind']=='import'] and classification!='cosmetic'):
                contracts.update(n['id'] for n in before['symbols'])
                changed.update(n['id'] for n in row['symbols'])
        for n in row['symbols']:
            n = dict(n,file_id=file_id)
            nodes[n['id']] = n
            previous = old_nodes.get(n['id'])
            if previous != n:
                fts_changed.add(n['id'])
            if previous and previous['body_sha']!=n['body_sha']:
                if previous.get('semantic_sha')==n.get('semantic_sha') or classification=='cosmetic':
                    cosmetic.append((previous,n))
                else:
                    changed.add(n['id'])
                    if classification=='contract': contracts.add(n['id'])
            elif not previous:
                changed.add(n['id'])
            elif previous['start_line']!=n['start_line']:
                cosmetic.append((previous,n))
    deleted = old_nodes.keys()-nodes.keys()
    before_candidates = {(n['id'],n['name'],n['qualified_name'],n['file_id']) for n in old_nodes.values()}
    after_candidates = {(n['id'],n['name'],n['qualified_name'],n['file_id']) for n in nodes.values()}
    return dict(results=results,nodes=nodes,old_nodes=old_nodes,changed=changed,contracts=contracts|deleted,
                fts_changed=fts_changed,cosmetic=cosmetic,affected_files=affected_files,deleted=deleted,
                affected_names={name for n in before_candidates^after_candidates for name in n[1:3]},
                removed_files={r['id'] for r in old.values()}-set(file_ids.values()),
                parsed_files=len(parsed),cache_hits=len(retained),unavailable_files=len(unavailable),renames=renames)


def _callers(db, targets):
    ids, found = list(targets),set()
    for offset in range(0,len(ids),400):
        page = ids[offset:offset+400]
        found.update(r[0] for r in db.execute("SELECT src_id FROM edges WHERE kind='call' AND dst_id IN ("+','.join('?' for _ in page)+')',page))
    return found


def _persist(db, plan, delta):
    """Persist only changed rows, retaining the surrounding transaction and old caller evidence."""
    from .structure import cache_key
    direct = _callers(db,delta['contracts'])
    for path,row in delta['results'].items():
        file_id,source = row['source_id'],plan['sources'][path]
        if file_id not in delta['affected_files']:
            continue
        db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            path=excluded.path,sha256=excluded.sha256,language=excluded.language,parser_id=excluded.parser_id,
            state=excluded.state,commit_id=excluded.commit_id,updated_at=excluded.updated_at,document=excluded.document''',
            (file_id,path,source['sha256'],source['language'],row['parser_id'],row['state'],plan['commit'],
             source['sha256']+':'+row['parser_id'],canonical({k:v for k,v in row.items() if k not in {'symbols','sites','text'}})))
        db.execute('INSERT OR REPLACE INTO structure_keys VALUES (?,?)',(cache_key(source),file_id))
        save_sites(db,file_id,row['sites'])
    db.execute('UPDATE files SET commit_id=? WHERE commit_id IS NOT ?',(plan['commit'],plan['commit']))
    for id in delta['fts_changed']:
        n = delta['nodes'][id]
        db.execute('''INSERT INTO nodes(id,file_id,kind,name,qualified_name,parent_id,start_line,end_line,signature,body_sha,updated_at,document)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            file_id=excluded.file_id,kind=excluded.kind,name=excluded.name,qualified_name=excluded.qualified_name,
            parent_id=excluded.parent_id,start_line=excluded.start_line,end_line=excluded.end_line,
            signature=excluded.signature,body_sha=excluded.body_sha,updated_at=excluded.updated_at,document=excluded.document''',
            (id,n['file_id'],n['kind'],n['name'],n['qualified_name'],n['parent_id'],n['start_line'],n['end_line'],
             n['signature'],n['body_sha'],n['body_sha'],canonical({k:v for k,v in n.items() if k!='file_id'})))
    for id in delta['deleted']:
        for table in ('semantics','semantic_tasks','nodes'):
            db.execute('DELETE FROM '+table+' WHERE '+('id' if table=='nodes' else 'node_id')+'=?',(id,))
    db.executemany('DELETE FROM files WHERE id=?',[(id,) for id in delta['removed_files']])
    if delta['affected_files'] or delta['removed_files'] or delta['deleted']:
        resolution = resolve(db,file_ids=delta['affected_files'],names=delta['affected_names'],
                             removed_sources=delta['deleted']|delta['removed_files'])
    else:
        resolution = dict(resolved_sites=0,written_edges=0,resolution_changed=set(),degree_changed=set())
    return direct,resolution


def invalidate(db, delta, direct_callers, resolution_changed=()):
    """Rebind equivalent source versions, then mark stale/suspect once, without recursion."""
    from . import skeleton as sk
    for before,after in delta['cosmetic']:
        sk._rebind_cosmetic(db,before,after)
    changed = delta['changed']
    suspect = (set(direct_callers)|set(resolution_changed)) & delta['nodes'].keys() - changed
    for id in changed:
        db.execute("UPDATE semantics SET status='stale' WHERE node_id=?",(id,))
        db.execute('DELETE FROM semantic_tasks WHERE node_id=?',(id,))
    for id in suspect:
        db.execute("UPDATE semantics SET status='suspect' WHERE node_id=? AND status='current'",(id,))
        db.execute('DELETE FROM semantic_tasks WHERE node_id=?',(id,))
    return suspect


def enqueue(db, delta, suspect, degree_changed=()):
    """Queue only affected semantic work and update only affected FTS rows."""
    from . import skeleton as sk
    touched = delta['fts_changed']|delta['changed']|suspect
    for id in touched:
        n = delta['nodes'][id]
        sem = sk._semantic(db,n)
        if sem and sem['status']=='current': continue
        degree = db.execute('SELECT degree FROM nodes WHERE id=?',(id,)).fetchone()[0]
        estimate = max(128,(n['end_byte']-n['start_byte']+2)//3+512)
        db.execute('INSERT OR IGNORE INTO semantic_tasks(node_id,body_sha,state,priority,estimated_tokens) VALUES (?,?,?,?,?)',
                   (id,n['body_sha'],'queued',degree*10-(1 if sem and sem['status']=='suspect' else 0),estimate))
    for id in set(degree_changed)&delta['nodes'].keys():
        db.execute('''UPDATE semantic_tasks SET priority=(SELECT degree*10 FROM nodes WHERE id=?)-
            CASE WHEN (SELECT status FROM semantics WHERE node_id=? ORDER BY created_at DESC LIMIT 1)='suspect' THEN 1 ELSE 0 END
            WHERE node_id=?''',(id,id,id))
    sk.update_search(db,touched|delta['deleted'])


def sync(store, **options):
    from . import skeleton as sk
    sk.initialize(store)
    plan = reconcile(store,**options)
    delta = rebind(plan)
    with sk.connection(store,write=True) as db:
        require(sk._meta(db,'version',0)==plan['version'],'Concurrent skeleton sync; retry')
        direct,resolution = _persist(db,plan,delta)
        suspect = invalidate(db,delta,direct,resolution['resolution_changed'])
        enqueue(db,delta,suspect,resolution['degree_changed'])
        for key,value in [('config',plan['config']),('root',str(plan['root'])),('budget_remaining',plan['budget']),('version',plan['version']+1)]:
            sk._save_meta(db,key,value)
        counts = {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('files','nodes','edges','semantic_tasks')}
    if plan['config']['legacy_cache']:
        from .structure import cache_key
        for path,row in delta['results'].items():
            if row['source_id']==plan['sources'][path]['id']:
                key = cache_key(plan['sources'][path])
                store.save_structure(key,dict(row,index_id=key))
    return {**counts,**{k:delta[k] for k in ('parsed_files','cache_hits','unavailable_files','renames')},
            'changed':sorted(delta['changed']),'deleted':sorted(delta['deleted']),'suspect':sorted(suspect),
            'resolved_sites':resolution['resolved_sites'],'written_edges':resolution['written_edges'],
            'budget_remaining':plan['budget'],'llm_tokens':0}
