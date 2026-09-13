"""Source-reviewed fixture for Move writing next + 1; not a general analyzer."""

from copy import deepcopy
from hashlib import sha256

from codemap.core import completion_report, require
from codemap.project import claim_next, commit_result
from codemap.repository import read_source
from examples.review_sample import BASE, EXPECTED


MOTION = (BASE / 'sample_repo/src/motion.cpp').read_text(encoding='utf-8').replace('state.position = next;', 'state.position = next + 1.0f;')


def review_plus_one(store):
    """Submit only affected files and their links, after verifying exact content."""
    initial = store.read()
    expected = {**EXPECTED, 'src/motion.cpp': sha256(MOTION.encode('utf-8')).hexdigest()}
    require({s['path']: s['sha256'] for s in initial['sources']} == expected, 'update fixture does not match this source snapshot')
    paths = {s['id']: s['path'] for s in initial['sources']}
    read_paths = set()
    while True:
        claimed = claim_next(store, 'verified-update-fixture')
        if claimed['state'] == 'no_ready_task':
            break
        graph = store.read()
        task = claimed['task']
        require(task['kind'] in {'update', 'link'}, 'initialize and review the original fixture first')
        for source in claimed['sources']:
            page = read_source(graph, graph['project']['source_root'], source['id'])
            require(page['next_start'] is None, 'fixture unexpectedly exceeds bounded reading')
            read_paths.add(source['path'])
            if source['path'] == 'src/motion.cpp':
                require(page['lines'][11]['text'].strip() == 'state.position = next + 1.0f;', 'updated write must be inspected')
        upserts = {}
        hashes = {s['id']: s['sha256'] for s in graph['sources']}
        for name, ids in task.get('review_ids', {}).items():
            rows = [deepcopy(row) for row in graph[name] if row['id'] in ids]
            for row in rows:
                row['freshness'] = 'current'
                if name == 'evidence':
                    row['sha256'] = hashes[row['source_id']]
                    if paths[row['source_id']] == 'src/motion.cpp' and row['id'] in {'ev:motion', 'ev:move'}:
                        row['note'] = 'Move 调用积分函数，再将 next + 1.0f 写入位置；积分返回值与最终写回值是两个数据身份。'
                elif name == 'entities':
                    if row['kind'] != 'external':
                        row['analysis'] = 'reviewed'
                    if row['id'] == 'fn:move':
                        row['summary'] = '调用积分函数后，将 next + 1.0f 写回 state.position；每次调用额外增加 1。'
                        row['details']['writes'] = ['state.position = next + 1.0f']
                        row['details']['outputs'] = ['state.position 的新值：积分结果 + 1.0f']
                    elif row['id'] == 'fn:main':
                        row['summary'] = '初始位置 1，按新实现第一次写回 3，第二次写回 6。源码中的断言仍要求 4；启用断言时条件不成立。此结论来自源码推导。'
                    elif row['kind'] == 'file' and any(paths[s] == 'tests/test_motion.cpp' for s in row['source_ids']):
                        row['summary'] = '两次 Move 按修改后的实现得到 6，断言条件仍为 4，需要同步调整测试预期。'
                    elif row['kind'] == 'file' and any(paths[s] == 'src/motion.cpp' for s in row['source_ids']):
                        row['summary'] = 'Move 先积分，再将积分结果加 1 写回；IntegratePosition 实现保持原样。'
            if rows:
                upserts[name] = rows
        if task['kind'] == 'update':
            upserts['sources'] = [dict(s, read_state='read', symbols_complete=True) for s in claimed['sources']]
        else:
            # A new arithmetic result must not be conflated with the callee return.
            all_flows = {f['id']: deepcopy(f) for f in graph['flows']}
            changed_flows = {f['id']: f for f in upserts.get('flows', [])}
            for number in (1, 2):
                id = f'flow:next-{number}'
                old = all_flows[id]
                intermediate = dict(old, id=f'flow:integrated-{number}', name=f'积分结果 · 调用 {number}',
                                    value_version=f'integrated@call{number}', color_key=f'D{7+number}', freshness='current')
                intermediate['derived_from'] = ['flow:initial' if number == 1 else 'flow:next-1', f'flow:velocity-{number}', f'flow:dt-{number}']
                intermediate['producer_port_id'] = f'p:ctx:integrate-{number}:fn:integrate:result:out'
                changed_flows[intermediate['id']] = intermediate
                result = changed_flows[id]
                result.update(producer_port_id=f'p:ctx:move-{number}:fn:move:written-position:out',
                              derived_from=[intermediate['id']], evidence_ids=['ev:move'])
                for relation in upserts['relations']:
                    if relation.get('flow_id') == id and relation['context_id'] == f'ctx:integrate-{number}':
                        relation['flow_id'] = intermediate['id']
                for port in upserts['ports']:
                    if port['id'] == result['producer_port_id']:
                        port['name'] = '写回 next + 1'
            upserts['flows'] = list(changed_flows.values())
            # Source-free summaries retain unaffected records and precise evidence.
            for row in upserts.get('entities', []):
                if not row['source_ids'] and row['kind'] != 'external':
                    row['summary'] = '已核对本次运动实现变更及相关测试。位置依次为 1、3、6，测试断言仍为 4；独立诊断与工具未受已记录依赖影响。'
                    row['evidence_ids'] = ['ev:move', 'ev:test']
        batch = claimed['batch_template']
        batch.update(upserts=upserts, result='done', reason='提交对精确更新样本的源码核对；新写回值和积分返回值分开记录，断言差异由源码推导。')
        commit_result(store, batch)
    return {'coverage': completion_report(store.read()), 'read_paths': sorted(read_paths)}
