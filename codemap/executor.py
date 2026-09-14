"""Host-neutral semantic claim/submit protocol, with transactional leases and budgets."""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4
from hashlib import sha256

from .core import canonical, require
from .skeleton import connection, _meta, _save_meta, refresh_search
from .repository import decode_text


class Executor(ABC):
    @abstractmethod
    def claim(self, n=1):
        """Return leased nodes, exact hashes, code and syntax context."""

    @abstractmethod
    def submit(self, results):
        """Atomically submit a batch bound to its leases and source hashes."""


class HostExecutor(Executor):
    def __init__(self, store, worker):
        self.store, self.worker = store, worker

    def claim(self, n=1):
        return claim(self.store, self.worker, n=n)

    def submit(self, results):
        return submit(self.store, results)


class CodexExecutor(HostExecutor):
    """Codex uses the same protocol as every other host; no CLI output parsing."""


def _disk_node(db, row):
    f = db.execute('SELECT * FROM files WHERE id=?',(row['file_id'],)).fetchone()
    root = Path(_meta(db,'root')).resolve()
    target = (root/f['path']).resolve(strict=True)
    require(target.is_relative_to(root),'Source escapes repository')
    raw = target.read_bytes()
    require(sha256(raw).hexdigest()==f['sha256'],'Source changed on disk; sync before claiming/submitting')
    node = json.loads(row['document'])
    snippet = decode_text(raw)[0].encode('utf-8')[node['start_byte']:node['end_byte']]
    require(sha256(snippet).hexdigest()==row['body_sha'],'Node bytes differ; sync first')
    return f['path'],snippet.decode('utf-8')


def claim(store, worker, n=1, lease_seconds=1800, now=None):
    require(isinstance(worker,str) and worker.strip(),'worker is required')
    require(type(n) is int and 1<=n<=20,'n must be 1..20')
    require(type(lease_seconds) is int and 1<=lease_seconds<=3600,'lease_seconds must be 1..3600')
    now = time.time() if now is None else now
    with connection(store,write=True) as db:
        # Reservations are not refunded: expired executors may already have spent tokens.
        db.execute("UPDATE semantic_tasks SET state='queued',lease_id=NULL,worker=NULL,expires_at=NULL WHERE state='running' AND expires_at<=?",(now,))
        remaining = _meta(db,'budget_remaining',0)
        tasks=[]
        candidates = db.execute("SELECT * FROM semantic_tasks WHERE state='queued' ORDER BY priority DESC,node_id").fetchall()
        for t in candidates:
            if len(tasks)>=n: break
            if t['estimated_tokens']>remaining: continue
            node = db.execute('SELECT * FROM nodes WHERE id=?',(t['node_id'],)).fetchone()
            path,code = _disk_node(db,node)
            lease = str(uuid4())
            db.execute("UPDATE semantic_tasks SET state='running',lease_id=?,worker=?,expires_at=?,generation=generation+1 WHERE node_id=?",(lease,worker,now+lease_seconds,t['node_id']))
            remaining-=t['estimated_tokens']
            context=[dict(r) for r in db.execute("SELECT * FROM edges WHERE kind='call' AND (src_id=? OR dst_id=?) ORDER BY src_id,dst_id,site_line LIMIT 100",(t['node_id'],t['node_id']))]
            tasks.append({'node_id':t['node_id'],'body_sha':t['body_sha'],'lease_id':lease,'worker':worker,
                          'expires_at':now+lease_seconds,'estimated_tokens':t['estimated_tokens'],
                          'path':path,'start_line':node['start_line'],'end_line':node['end_line'],
                          'signature':node['signature'],'source':code,'calls':context,
                          'output_fields':['summary','detail','evidence','model','used_tokens']})
        _save_meta(db,'budget_remaining',remaining)
        return {'tasks':tasks,'budget_remaining':remaining,'state':'claimed' if tasks else 'budget_exhausted_or_idle'}


def submit(store, batch, now=None):
    require(isinstance(batch,dict) and isinstance(batch.get('batch_id'),str) and batch['batch_id'],'batch_id is required')
    require(isinstance(batch.get('results'),list) and 1<=len(batch['results'])<=20,'results must contain 1..20 items')
    payload=canonical(batch)
    now=time.time() if now is None else now
    with connection(store,write=True) as db:
        receipt=db.execute('SELECT payload FROM semantic_receipts WHERE batch_id=?',(batch['batch_id'],)).fetchone()
        if receipt:
            require(receipt[0]==payload,'batch_id reused with different payload')
            return {'accepted':len(batch['results']),'reused':True}
        seen=set()
        for result in batch['results']:
            require(isinstance(result,dict),'Each result must be an object')
            id=result.get('node_id')
            require(id not in seen,'Duplicate result node')
            seen.add(id)
            t=db.execute('SELECT * FROM semantic_tasks WHERE node_id=?',(id,)).fetchone()
            require(t is not None and t['state']=='running' and t['lease_id']==result.get('lease_id') and t['expires_at']>now,'Lease missing, expired or replaced')
            node=db.execute('SELECT * FROM nodes WHERE id=?',(id,)).fetchone()
            require(node['body_sha']==result.get('body_sha')==t['body_sha'],'Semantic result hash does not match leased node')
            _disk_node(db,node)
            require(isinstance(result.get('summary'),str) and result['summary'].strip(),'summary is required')
            require(isinstance(result.get('detail'),dict),'detail must be an object')
            require(isinstance(result.get('evidence'),list) and result['evidence'],'evidence is required')
            for evidence in result['evidence']:
                require(isinstance(evidence,dict) and evidence.get('node_id')==id and evidence.get('body_sha')==node['body_sha'],'Evidence must bind node_id and body_sha')
                require(type(evidence.get('start_line')) is int and type(evidence.get('end_line')) is int and
                        node['start_line']<=evidence['start_line']<=evidence['end_line']<=node['end_line'],'Evidence range must be inside node')
            tokens=result.get('used_tokens')
            require(type(tokens) is int and 0<=tokens<=t['estimated_tokens'],'used_tokens must fit the reserved task budget')
            require(isinstance(result.get('model'),str) and result['model'],'model/executor identity is required')
            stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()+':'+batch['batch_id']
            db.execute('INSERT INTO semantics VALUES (?,?,?,?,?,?,?,?)',(id,node['body_sha'],'current',result['summary'],canonical(result['detail']),canonical(result['evidence']),result['model'],stamp))
            db.execute("UPDATE semantic_tasks SET state='done',lease_id=NULL,expires_at=NULL WHERE node_id=?",(id,))
            _save_meta(db,'budget_remaining',_meta(db,'budget_remaining',0)+t['estimated_tokens']-tokens)
        refresh_search(db)
        db.execute('INSERT INTO semantic_receipts VALUES (?,?)',(batch['batch_id'],payload))
        return {'accepted':len(batch['results']),'reused':False,'budget_remaining':_meta(db,'budget_remaining',0)}
