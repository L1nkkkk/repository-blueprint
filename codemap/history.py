"""Graph history comparisons and guarded, reversible restoration to today's sources."""
from copy import deepcopy
from difflib import unified_diff
import time

from .store import encode_source
from .core import TABLES, canonical, digest, require, validate_graph
from .change_analysis import snapshot_texts
from .updates import scan_update, update_plan, migrate_snapshot


def record_label(graph, table, row):
    if table == 'sources':
        return row['path']
    if table == 'entities':
        return row['name']
    entities = {e['id']: e['name'] for e in graph['entities']}
    ports = {p['id']: entities.get(p['entity_id'], '') + '.' + p['name'] for p in graph['ports']}
    if table == 'relations':
        return f"{ports.get(row['from_id'], entities.get(row['from_id'], row['from_id']))} → {ports.get(row['to_id'], entities.get(row['to_id'], row['to_id']))}"
    return row.get('name') or row.get('label') or row['id']


def compare_graphs(before, after, limit=160):
    changes = {}
    for table in ('sources', 'entities', 'relations', 'flows'):
        old, new = ({r['id']: r for r in g[table]} for g in (before, after))
        added = [id for id in new if id not in old]
        deleted = [id for id in old if id not in new]
        modified = [id for id in new if id in old and new[id] != old[id]]
        items = []
        for kind, ids in (('added', added), ('deleted', deleted), ('modified', modified)):
            for id in ids:
                if len(items) >= limit:
                    break
                a, b = old.get(id), new.get(id)
                items.append({'id': id, 'kind': kind, 'before': record_label(before, table, a) if a else None,
                              'after': record_label(after, table, b) if b else None,
                              'before_summary': (a or {}).get('summary', ''), 'after_summary': (b or {}).get('summary', ''),
                              'fields': sorted(k for k in set(a or {}) | set(b or {}) if (a or {}).get(k) != (b or {}).get(k))})
        changes[table] = {'added': len(added), 'deleted': len(deleted), 'modified': len(modified),
                          'items': items, 'truncated': len(added) + len(deleted) + len(modified) > len(items)}
    return changes


def restore_preview(store, revision):
    current, archived = store.read(), store.read_snapshot(revision)
    require(current['project']['id'] == archived['project']['id'], 'History belongs to a different project.')
    archived['project']['source_root'] = current['project']['source_root']
    fresh = scan_update(archived)
    current_sources = {s['id']: s for s in current['sources']}
    old_paths, fresh_paths = ({s['path'] for s in g['sources']} for g in (archived, fresh))
    # Preserve a move already established in this map, including edited moves.
    renames = [{'from': s['path'], 'to': current_sources[s['id']]['path']} for s in archived['sources']
               if s['id'] in current_sources and s['path'] not in fresh_paths
               and current_sources[s['id']]['path'] in fresh_paths - old_paths]
    old_texts = store.source_texts(s['sha256'] for s in archived['sources'])
    new_texts = snapshot_texts(fresh)
    plan = update_plan(archived, fresh, old_texts=old_texts, new_texts=new_texts, renames=renames)
    result = {'revision': revision, 'current_revision': current['project']['revision'],
              'changes': compare_graphs(archived, current), 'source_update': plan,
              'can_restore': plan['can_apply'], 'scope_note': '恢复分析图谱，不修改仓库文件。当前图谱会先归档，布局保留；与现有源码不一致的内容将重新排队核对。'}
    result['blocked_reason'] = '' if plan['can_apply'] else '源码范围不完整，暂不能恢复。'
    with store._connection() as db:
        reader_running = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reader_requests'").fetchone()
                              and db.execute("SELECT 1 FROM reader_requests WHERE state='running'").fetchone())
    if reader_running or any(t['state'] == 'running' and t['lease']['expires_at'] > time.time() for t in current['tasks']):
        result.update(can_restore=False, blocked_reason='请先暂停正在运行的阅读者，再重新比较并恢复历史。')
    result['restore_id'] = digest({'revision': revision, 'current': current['project']['revision'],
                                   'source_plan': plan['plan_id']})
    # Only provide historical text when its exact hash was actually archived.
    texts = store.source_texts(s['sha256'] for g in (archived, current) for s in g['sources'])
    old_sources, new_sources = ({s['id']: s for s in g['sources']} for g in (archived, current))
    diffs = []
    for row in result['changes']['sources']['items'][:20]:
        a, b = old_sources.get(row['id']), new_sources.get(row['id'])
        old = texts.get(a['sha256']) if a else ''
        new = texts.get(b['sha256']) if b else ''
        available = old is not None and new is not None
        lines = list(unified_diff((old or '').splitlines(), (new or '').splitlines(),
                                  fromfile=(a or {}).get('path', '未存在'), tofile=(b or {}).get('path', '未存在'), lineterm='')) if available else []
        diffs.append({'path': (b or a)['path'], 'available': available, 'diff': '\n'.join(lines[:160])[:24000],
                      'truncated': len(lines) > 160 or len('\n'.join(lines[:160])) > 24000})
    result['source_diffs'] = diffs
    return result


def restore_snapshot(store, revision, *, expected_revision, restore_id):
    current = store.read()
    previous = current.get('last_restore', {})
    if previous.get('restore_id') == restore_id:
        require(previous['revision'] == revision and previous['previous_revision'] == expected_revision, '恢复标识已经用于另一操作。')
        return {'revision': previous['applied_revision'], 'restored_from': revision, 'archived_revision': expected_revision, 'reused': True}
    preview = restore_preview(store, revision)
    require(preview['current_revision'] == expected_revision, '图谱已经变化，请重新比较后恢复。')
    require(preview['restore_id'] == restore_id, '源码或恢复方案已经变化，请重新比较。')
    require(preview['can_restore'], preview['blocked_reason'])
    archived = store.read_snapshot(revision)
    archived['project']['source_root'] = store.read()['project']['source_root']
    fresh = scan_update(archived)
    old_texts, new_texts = store.source_texts(s['sha256'] for s in archived['sources']), snapshot_texts(fresh)
    # Recheck the source plan immediately before the atomic write.
    candidate = migrate_snapshot(archived, fresh, expected_revision=archived['project']['revision'],
                                  plan_id=preview['source_update']['plan_id'], old_texts=old_texts, new_texts=new_texts,
                                  renames=preview['source_update']['rename_approvals'])
    for task in candidate['tasks']:
        if task['state'] == 'running':
            task.update(state='queued', lease=None, reason='历史恢复后需要重新领取任务。')
    candidate['project']['revision'] = expected_revision + 1
    candidate.pop('last_update', None)
    candidate['last_restore'] = {'revision': revision, 'previous_revision': expected_revision, 'applied_revision': expected_revision + 1, 'restored_at': time.time(), 'restore_id': restore_id}
    validate_graph(candidate)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        import json
        current = json.loads(db.execute('SELECT document FROM graph WHERE id=1').fetchone()[0])
        require(current['project']['revision'] == expected_revision, '图谱已经变化，请重新比较后恢复。')
        require(not any(t['state'] == 'running' and t['lease']['expires_at'] > time.time() for t in current['tasks']), '请先暂停正在运行的阅读者，再恢复历史。')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reader_requests'").fetchone():
            require(not db.execute("SELECT 1 FROM reader_requests WHERE state='running'").fetchone(), '请先暂停正在执行的画布请求。')
        db.execute('INSERT INTO snapshot_history VALUES (?, ?, ?)', (expected_revision, current['project']['snapshot_id'], canonical(current)))
        db.execute('UPDATE graph SET document=? WHERE id=1', (canonical(candidate),))
        db.executemany('INSERT OR IGNORE INTO source_texts VALUES (?, ?)', ((key,encode_source(value)) for key,value in new_texts.items()))
    return {'revision': candidate['project']['revision'], 'restored_from': revision, 'archived_revision': expected_revision,
            'scope_note': preview['scope_note']}
