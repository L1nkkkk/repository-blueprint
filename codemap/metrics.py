"""Local MCP observations, separate from graph facts, revisions and task leases.

Only metadata is retained: never source text, prompts, arguments or responses.
Tool time uses a monotonic clock. Between-call time is NOT model reasoning time.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

from .core import require

FILENAME = 'review-metrics.sqlite'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, client TEXT NOT NULL, started REAL NOT NULL, closed REAL, dropped INTEGER NOT NULL DEFAULT 0,
 project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, manifest_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
 id INTEGER PRIMARY KEY, session TEXT NOT NULL, tool TEXT NOT NULL, phase TEXT NOT NULL,
 started REAL NOT NULL, ended REAL, elapsed_ms REAL, gap_ms REAL, status TEXT NOT NULL,
 fingerprint TEXT NOT NULL, input_bytes INTEGER NOT NULL, output_bytes INTEGER NOT NULL DEFAULT 0,
 task_id TEXT, target TEXT, snapshot TEXT, revision INTEGER, batch_id TEXT, outcome TEXT,
 read_lines INTEGER NOT NULL DEFAULT 0, repeated_lines INTEGER NOT NULL DEFAULT 0,
 repeated_request INTEGER NOT NULL DEFAULT 0, retry INTEGER NOT NULL DEFAULT 0, error_kind TEXT);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session,id);
CREATE INDEX IF NOT EXISTS calls_fingerprint ON calls(session,fingerprint,id);
'''
PHASES = {'init': 'inventory', 'index': 'structure', 'next': 'claim_pack', 'pack': 'material',
          'read': 'material', 'search': 'material', 'context': 'query', 'query': 'query',
          'prepare': 'prepare', 'commit': 'commit', 'status': 'status', 'task': 'control',
          'update': 'update', 'canvas': 'other', 'export': 'other'}
SCOPE_NOTE = ('从新版工具首次访问地图时开始记录；旧进程和此前耗时不能补记。'
              '工具耗时不含统计写入、模型启动及网络传输；调用间隔可能包含模型处理、网络等待、人工停顿或其他工作，不能当作纯推理耗时。'
              '字节数为参数和结果的 UTF-8 JSON 大小，不是 Token 数。统计不会改变审阅完成状态。')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def short(value, limit=240):
    return value[:limit] if isinstance(value, str) else None


def identity(value):
    if not isinstance(value, str):
        return None
    return value if len(value) <= 240 else 'sha256:' + sha256(value.encode('utf-8')).hexdigest()


def manifest_hash(graph):
    return sha256(encoded(sorted((s['path'], s['sha256'], s['included']) for s in graph['sources']))).hexdigest()


def failure_kind(error):
    if error is None:
        return None
    message = str(error).lower()
    for kind, words in [('revision', ('stale project revision', 'graph revision changed', 'plan changed')),
                        ('lease', ('lease', 'execution identity', '执行身份')),
                        ('source', ('snapshot', 'fingerprint', 'hash', 'source changed', 'source is no longer')),
                        ('evidence', ('evidence', 'read these lines', 'read the entire', 'analysis depth'))]:
        if any(word in message for word in words):
            return kind
    return 'io' if isinstance(error, OSError) else 'validation' if isinstance(error, ValueError) else 'tool'


@contextmanager
def connect(path, *, readonly=False):
    db = sqlite3.connect(Path(path).as_uri() + '?mode=ro' if readonly else str(path),
                         uri=readonly, timeout=.05)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def add_range(ranges, key, start, end):
    """Count overlaps against already returned complete lines at the same hash."""
    old = ranges.get(key, [])
    overlap = sum(max(0, min(end, b) - max(start, a) + 1) for a, b in old)
    merged = []
    for a, b in sorted([*old, (start, end)]):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    ranges[key] = merged
    return end - start + 1, overlap


class Recorder:
    def __init__(self, *, clock=time.perf_counter, wall=time.time):
        self.id = uuid4().hex
        self.client = '未报告宿主'
        self.clock, self.wall = clock, wall
        self.maps = {}

    def set_client(self, info):
        self.client = short(info.get('name'), 100) or '未报告宿主'

    def safe(self, state, operation):
        try:
            return operation()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            state['dropped'] += 1
            if state['dropped'] == 1:
                print('Blueprint timing unavailable; analysis continues (' + type(exc).__name__ + ').', file=sys.stderr)
            return None

    def begin(self, store, tool, args, *, started=None, start_clock=None):
        path = Path(store.path).resolve().with_name(FILENAME)
        state = self.maps.setdefault(path, {'ready': False, 'dropped': 0, 'ranges': {}, 'last_end': None, 'task': None})
        span = Span(self, path, state, tool, args, started=started, start_clock=start_clock)
        def insert():
            with connect(path) as db:
                if not state['ready']:
                    db.executescript(SCHEMA)
                    graph = store.read()
                    db.execute('INSERT OR IGNORE INTO sessions(id,client,started,project_id,snapshot_id,manifest_hash) VALUES (?,?,?,?,?,?)',
                               (self.id, self.client, span.started, graph['project']['id'], graph['project']['snapshot_id'], manifest_hash(graph)))
                previous = db.execute('SELECT status FROM calls WHERE session=? AND fingerprint=? ORDER BY id DESC LIMIT 1',
                                      (self.id, span.fingerprint)).fetchone()
                row = db.execute('INSERT INTO calls(session,tool,phase,started,gap_ms,status,fingerprint,input_bytes,task_id,target,repeated_request,retry) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.id, tool, PHASES.get(tool, 'other'), span.started, span.gap_ms, 'unfinished', span.fingerprint,
                     span.input_bytes, span.task_id, span.target, int(previous is not None), int(previous is not None and previous['status'] == 'error')))
                span.row_id = row.lastrowid
            state['ready'] = True
        self.safe(state, insert)
        # Database and metadata overhead are excluded from actual tool execution.
        span.start_clock = self.clock()
        return span

    def close(self):
        for path, state in self.maps.items():
            if state['ready']:
                def close_session():
                    with connect(path) as db:
                        db.execute('UPDATE sessions SET closed=?,dropped=? WHERE id=?', (self.wall(), state['dropped'], self.id))
                self.safe(state, close_session)


class Span:
    def __init__(self, recorder, path, state, tool, args, *, started=None, start_clock=None):
        self.recorder, self.path, self.state, self.tool = recorder, path, state, tool
        measured = recorder.clock()
        self.started = recorder.wall() if started is None else started
        self.start_clock = measured if start_clock is None else start_clock
        self.setup_ms = max(0, (measured - self.start_clock) * 1000)
        self.gap_ms = None if state['last_end'] is None else max(0, (self.start_clock - state['last_end']) * 1000)
        payload = encoded(args)
        self.fingerprint, self.input_bytes = sha256(tool.encode() + payload).hexdigest(), len(payload)
        batch = args.get('batch') or args.get('batch_template') or {}
        self.task_id = identity(args.get('task_id') or batch.get('task_id') or state['task'])
        self.target = short(args.get('source') or args.get('entity_id') or self.task_id)
        self.batch_id, self.outcome, self.row_id = None, None, None

    def batch(self, batch):
        self.task_id = identity(batch.get('task_id'))
        self.outcome = short(batch.get('result'))

    def finish(self, output=None, error=None):
        elapsed = self.setup_ms + max(0, (self.recorder.clock() - self.start_clock) * 1000)
        ended = self.recorder.wall()
        def save():
            result = output if isinstance(output, dict) else {}
            if self.tool == 'next' and result.get('state') == 'claimed':
                self.task_id = identity(result['task']['id'])
                self.state['task'] = self.task_id
                self.target = short(', '.join(s['path'] for s in result.get('sources', [])[:3])) or self.task_id
            elif self.tool == 'commit' and error is None:
                self.batch_id = identity(result.get('receipt', {}).get('batch_id'))
                if self.state['task'] == self.task_id:
                    self.state['task'] = None
            reads = [result] if self.tool == 'read' and error is None else []
            pack = result.get('reading_pack', {}) if self.tool == 'next' else result if self.tool == 'pack' else {}
            reads.extend(i for i in pack.get('items', []) if i.get('kind') == 'source')
            count, repeated = 0, 0
            for item in reads:
                if item.get('lines'):
                    n, overlap = add_range(self.state['ranges'], (item['source_id'], item['sha256']), item['start_line'], item['end_line'])
                    count += n; repeated += overlap
            project = result.get('project', {})
            snapshot = short(project.get('snapshot_id') or result.get('snapshot_id'))
            revision = project.get('revision', result.get('revision'))
            with connect(self.path) as db:
                db.execute('UPDATE calls SET ended=?,elapsed_ms=?,status=?,output_bytes=?,task_id=?,target=?,snapshot=?,revision=?,batch_id=?,outcome=?,read_lines=?,repeated_lines=?,error_kind=? WHERE id=?',
                    (ended, elapsed, 'error' if error else 'ok', len(encoded(result)) if error is None else 0,
                     self.task_id, self.target, snapshot, revision, self.batch_id, self.outcome, count, repeated,
                     failure_kind(error), self.row_id))
                db.execute('UPDATE sessions SET dropped=? WHERE id=?', (self.state['dropped'], self.recorder.id))
        if self.row_id is not None:
            self.recorder.safe(self.state, save)
        # Exclude recording overhead from the following between-call interval.
        self.state['last_end'] = self.recorder.clock()


def report(store, *, session=None, limit=40, now=None):
    require(isinstance(session, (str, type(None))) and (session is None or 1 <= len(session) <= 100), 'invalid metrics session')
    require(type(limit) is int and 1 <= limit <= 100, 'metrics limit must be 1–100')
    graph = store.read()
    paths = {s['id']: s['path'] for s in graph['sources']}
    running = [t for t in graph['tasks'] if t['state'] == 'running']
    result = {'format_version': 1, 'state': 'empty', 'scope_note': SCOPE_NOTE, 'sessions': [], 'selected': None,
        'summary': None, 'phases': [], 'tasks': [], 'events': [], 'generated_at': time.time() if now is None else now,
        'project': {**{k: graph['project'].get(k) for k in ('id', 'name', 'revision', 'snapshot_id')}, 'manifest_hash': manifest_hash(graph)},
        'progress': {'sources_read': sum(s['read_state'] == 'read' for s in graph['sources'] if s['included']),
                     'sources_total': sum(s['included'] for s in graph['sources']),
                     'task_counts': {state: sum(t['state'] == state for t in graph['tasks']) for state in ('queued', 'running', 'done', 'blocked')},
                     'claimed_tasks': [{'id': t['id'], 'worker': t['lease']['worker'], 'expires_at': t['lease']['expires_at'],
                        'paths': [paths[id] for id in t['source_ids'] if id in paths][:5]} for t in running[:20]],
                     'claimed_task_count': len(running)}}
    path = Path(store.path).resolve().with_name(FILENAME)
    if not path.is_file():
        return result
    try:
        with connect(path, readonly=True) as db:
            result['session_count'] = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
            result['sessions'] = [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY started DESC,id DESC LIMIT 50')]
            if not result['sessions']:
                return result
            chosen = session or result['sessions'][0]['id']
            selected = db.execute('SELECT * FROM sessions WHERE id=?', (chosen,)).fetchone()
            require(selected is not None, 'metrics session not found')
            result.update(state='recorded', selected=dict(selected))
            result['snapshot_matches_start'] = selected['manifest_hash'] == result['project']['manifest_hash']
            sums = '''COUNT(*) AS calls, SUM(status='error') AS errors, SUM(status='unfinished') AS unfinished,
                COALESCE(SUM(elapsed_ms),0) AS tool_ms, COALESCE(SUM(gap_ms),0) AS between_calls_ms,
                SUM(input_bytes) AS input_bytes, SUM(output_bytes) AS output_bytes,
                SUM(read_lines) AS read_lines, SUM(repeated_lines) AS repeated_lines,
                SUM(repeated_request) AS repeated_requests, SUM(retry) AS retries,
                COUNT(DISTINCT batch_id) AS observed_batches, SUM(batch_id IS NOT NULL) AS successful_commit_calls,
                MIN(started) AS first_call_at, MAX(COALESCE(ended,started)) AS last_activity_at'''
            result['summary'] = dict(db.execute('SELECT ' + sums + ' FROM calls WHERE session=?', (chosen,)).fetchone())
            result['summary']['quiet_seconds'] = max(0, result['generated_at'] - (result['summary']['last_activity_at'] or selected['started']))
            result['phases'] = [dict(r) for r in db.execute('SELECT phase, COUNT(*) AS calls, SUM(status=\'error\') AS errors, COALESCE(SUM(elapsed_ms),0) AS tool_ms FROM calls WHERE session=? GROUP BY phase ORDER BY tool_ms DESC', (chosen,))]
            result['tasks'] = [dict(r) for r in db.execute('SELECT task_id,COUNT(*) AS calls,COALESCE(SUM(elapsed_ms),0) AS tool_ms,COUNT(DISTINCT batch_id) AS observed_batches,MIN(started) AS first_call_at,MAX(COALESCE(ended,started)) AS last_activity_at FROM calls WHERE session=? AND task_id IS NOT NULL GROUP BY task_id ORDER BY tool_ms DESC LIMIT 30', (chosen,))]
            result['events'] = [dict(r) for r in db.execute('SELECT id,tool,phase,started,ended,elapsed_ms,gap_ms,status,task_id,target,input_bytes,output_bytes,read_lines,repeated_lines,retry,batch_id,outcome,error_kind FROM calls WHERE session=? ORDER BY id DESC LIMIT ?', (chosen, limit))]
            result['events_truncated'] = result['summary']['calls'] > len(result['events'])
            task_rows = {identity(t['id']): t for t in graph['tasks']}
            for row in result['tasks']:
                task = task_rows.get(row['task_id'])
                row['paths'] = [paths[id] for id in task['source_ids'] if id in paths][:5] if task else []
                row['current_state'] = task['state'] if task else 'not_in_current_graph'
            return result
    except sqlite3.Error:
        return {**result, 'state': 'unavailable', 'message': '统计文件暂时无法读取；分析和图谱不受影响。'}
