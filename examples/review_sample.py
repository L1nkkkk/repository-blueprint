"""Replay the source review authored for this exact five-file sample.

This is a reproducible review fixture, NOT a general-purpose AI analyzer.
Hashes pin every finding to the source inspected during its authoring.
"""

from copy import deepcopy
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from codemap.core import require, completion_report
from codemap.repository import identity, entity, read_source
from codemap.project import initialize_project, open_project, claim_next, commit_result, export_graph

EXPECTED = {
    'CMakeLists.txt': '41e89b167a4ee37bd132046f9dc9a0538d5bcbb643fe83e7582977064f9239bc',
    'src/diagnostics.cpp': 'ba836010c3affcfaa071124959279a3f090769605e1959ab2f5fe40387f85680',
    'src/motion.cpp': '8d423b6bb71bfde3c88b582a5ea790170c9d7f378e98c09f82e494eb13660ba3',
    'tests/test_motion.cpp': '466281b6686fa757697f9ae20c031b0c1817357ad4e487daa3f1186dc486d5c0',
    'tools/report.cpp': 'ffbedb2bfcbd99ee60a91522fb08b22f6fac3cf00e3fc385fb1c1b7c2fb3b577',
}


def reviewed_graph(initial):
    g = deepcopy(initial)
    require({s['path']: s['sha256'] for s in g['sources']} == EXPECTED, 'review fixture does not match this exact repository snapshot')
    by_path = {s['path']: s for s in g['sources']}

    def proof(id, path, start, end, note):
        s = by_path[path]
        g['evidence'].append({'id': id, 'source_id': s['id'], 'sha256': s['sha256'], 'start_line': start, 'end_line': end, 'note': note})
        return id

    build = proof('ev:build', 'CMakeLists.txt', 1, 5, '构建入口、独立诊断编译单元与工具目标')
    motion = proof('ev:motion', 'src/motion.cpp', 1, 15, '运动命名空间、状态结构与方法实现')
    integrate = proof('ev:integrate', 'src/motion.cpp', 4, 6, '位置加速度乘时间的结果；函数无状态写入')
    move = proof('ev:move', 'src/motion.cpp', 10, 13, '读取旧位置，调用积分函数，写回同一 State 对象')
    integrate_call = proof('ev:call-integrate', 'src/motion.cpp', 11, 11, '调用 IntegratePosition 的具体位置；父上下文区分两次 Move')
    field = proof('ev:field', 'src/motion.cpp', 2, 2, 'State.position 字段声明')
    measure = proof('ev:measure', 'src/diagnostics.cpp', 1, 5, '独立诊断函数计算平方；仓库中没有对它的调用')
    test = proof('ev:test', 'tests/test_motion.cpp', 1, 10, '包含运动实现，初始化状态，连续调用两次并断言')
    first = proof('ev:call-first', 'tests/test_motion.cpp', 7, 7, '第一次 Move 调用：2.0f、0.5f、同一个 state')
    second = proof('ev:call-second', 'tests/test_motion.cpp', 8, 8, '第二次 Move 调用：4.0f、0.5f、第一次写回后的 state')
    assertion = proof('ev:assert', 'tests/test_motion.cpp', 9, 9, '断言最终位置等于 4.0f；启用情况取决于 NDEBUG')
    report = proof('ev:report', 'tools/report.cpp', 1, 5, '独立工具函数将厘米除以 100 转为显示单位')
    summary = {
        'CMakeLists.txt': ('声明 motion_sample 可执行文件及 report_tool 对象库；运动实现由测试直接包含，避免重复编译。', build),
        'src/motion.cpp': ('定义位置状态与运动系统。Move 读取位置，调用 IntegratePosition，再将结果写回传入的 State。', motion),
        'src/diagnostics.cpp': ('独立诊断模块，Measure 返回输入的平方。已核对全部样本，当前没有仓库内调用者。', measure),
        'tests/test_motion.cpp': ('测试入口：初始位置为 1，两次移动后按源码计算得到 4，随后断言。此结论来自源码推导，未声称实际运行。', test),
        'tools/report.cpp': ('独立工具代码，DisplayUnits 将厘米换算为显示单位；当前没有仓库内调用者。', report),
    }
    for s in g['sources']:
        s.update(read_state='read', symbols_complete=True)
    for e in g['entities']:
        if e['kind'] == 'file':
            path = next(s['path'] for s in g['sources'] if s['id'] in e['source_ids'])
            e.update(analysis='reviewed', summary=summary[path][0], evidence_ids=[summary[path][1]])
        else:
            name = e['name']
            note = {'src': '运动与独立诊断实现。诊断函数虽没有入口调用，仍已逐行阅读。', 'tests': '独立测试入口，包含连续两次调用和结果断言。', 'tools': '单独构建的显示单位换算工具，已纳入全仓库范围。'}.get(name, '小型 C++ 仓库，包含运动实现、诊断、测试与工具。全仓库五个文件均已核对；标准库 assert 保留外部边界。')
            e.update(analysis='reviewed', summary=note, evidence_ids=[build])

    def symbol(id, kind, name, path, note, evidence, parent, roles=None, **details):
        row = entity(kind, id, name, [by_path[path]['id']], 'cpp', roles)
        row.update(id=id, analysis='reviewed', summary=note, evidence_ids=[evidence])
        if kind in {'function', 'method'}:
            row['details'] = {key: details.get(key, []) for key in ('inputs','outputs','calls','reads','writes','conditions')}
        g['entities'].append(row)
        g['memberships'].append({'id':'member:'+id,'axis':'semantic','parent_id':parent,'child_id':id})

    symbol('scope:motion','namespace','motion','src/motion.cpp','位置状态和运动计算的语义分组。',motion,identity('file','src/motion.cpp'))
    symbol('type:state','struct','State','src/motion.cpp','保存可写的位置值。',field,'scope:motion')
    symbol('field:position','field','State.position','src/motion.cpp','float 位置字段；Move 读取旧值并写入新的积分结果。',field,'type:state')
    symbol('type:movement','class','MovementSystem','src/motion.cpp','无自有字段的运动方法容器。状态通过引用参数传入。',motion,'scope:motion')
    symbol('fn:integrate','function','IntegratePosition','src/motion.cpp','返回 position + velocity * dt；三个参数按值输入，无外部状态读写。',integrate,'scope:motion',inputs=['position: float','velocity: float','dt: float'],outputs=['float: position + velocity * dt'])
    symbol('fn:move','method','Move','src/motion.cpp','将速度和时间传给积分函数，并把结果写回 state.position。返回类型为 void，数据结果通过引用状态传出。',move,'type:movement',inputs=['velocity: float','dt: float','state: State&'],outputs=['state.position 的新值（引用写回）'],calls=['IntegratePosition(state.position, velocity, dt)'],reads=['state.position 的调用前版本'],writes=['state.position = next'])
    symbol('scope:diagnostics','namespace','diagnostics','src/diagnostics.cpp','诊断代码的独立语义分组。',measure,identity('file','src/diagnostics.cpp'))
    symbol('fn:measure','function','Measure','src/diagnostics.cpp','返回 value * value，无副作用。当前完整样本内没有调用点；输入域为任意 float，未声明额外边界保证。',measure,'scope:diagnostics',inputs=['value: float'],outputs=['float: value 的平方'])
    symbol('fn:main','function','main','tests/test_motion.cpp','先将 state.position 设为 1。第一次调用后位置为 2，第二次后为 4。两个调用共享同一 state，但使用不同的速度值。assert 受 NDEBUG 影响；main 正常结束隐式返回 0。',test,identity('file','tests/test_motion.cpp'),roles=['entry','test'],outputs=['正常退出码 0；启用断言且条件失败时由外部断言机制终止'],calls=['Move(2.0f, 0.5f, state) @ 第 7 行','Move(4.0f, 0.5f, state) @ 第 8 行','assert(state.position == 4.0f) @ 第 9 行（宏边界）'],reads=['第二次调用接收第一次写回的 state','断言读取最终 state.position'],writes=['初始化 state.position = 1.0f','两次 Move 的引用写回'],conditions=['断言条件：state.position == 4.0f；NDEBUG 定义时断言可被移除'])
    symbol('scope:reporting','namespace','reporting','tools/report.cpp','工具换算的语义分组。',report,identity('file','tools/report.cpp'),roles=['tool'])
    symbol('fn:display','function','DisplayUnits','tools/report.cpp','返回 centimeters / 100.0f，无外部状态或副作用；没有仓库内调用点。',report,'scope:reporting',roles=['tool'],inputs=['centimeters: float'],outputs=['float: centimeters / 100.0f'])
    external = entity('external','assert','assert / <cassert>')
    external.update(id='external:assert',summary='标准库断言宏边界。这里只记录源码引用和输入条件，不把宏展开虚构为普通函数调用。')
    g['entities'].append(external)
    g['contexts'] = [{'id':'ctx:main','parent_id':None,'caller_id':None,'callee_id':None}, {'id':'ctx:measure','parent_id':None,'caller_id':None,'callee_id':None}, {'id':'ctx:display','parent_id':None,'caller_id':None,'callee_id':None}]

    def port(owner, context, name, direction, version):
        id = f'p:{context}:{owner}:{version}:{direction}'
        g['ports'].append({'id':id,'entity_id':owner,'context_id':context,'direction':direction,'channel':'data','name':name,'type':'float'})
        return id

    def flow(id, name, producer, key, version, derived, ev):
        g['flows'].append({'id':id,'name':name,'producer_port_id':producer,'color_key':key,'value_version':version,'derived_from':derived,'evidence_ids':[ev]})

    def edge(a,b,f,context,ev,kind='data'):
        g['relations'].append({'id':f'rel:{len(g["relations"])}','kind':kind,'from_id':a,'to_id':b,'flow_id':f,'context_id':context,'basis':'source','freshness':'current','evidence_ids':[ev]})

    def code_edge(a,b,context,ev,kind='call'):
        g['relations'].append({'id':f'rel:{len(g["relations"])}','kind':kind,'from_id':a,'to_id':b,'context_id':context,'basis':'source','freshness':'current','evidence_ids':[ev]})

    position = port('fn:main','ctx:main','state.position = 1','out','position-initial')
    flow('flow:initial','初始位置',position,'D1','state#main.position@initial',[],test)
    for number, ev, speed in [(1,first,'2.0'),(2,second,'4.0')]:
        ctx, inner = f'ctx:move-{number}', f'ctx:integrate-{number}'
        g['contexts'].append({'id':ctx,'parent_id':'ctx:main','caller_id':'fn:main','callee_id':'fn:move','callsite_evidence_id':ev})
        g['contexts'].append({'id':inner,'parent_id':ctx,'caller_id':'fn:move','callee_id':'fn:integrate','callsite_evidence_id':integrate_call})
        code_edge('fn:main','fn:move',ctx,ev);code_edge('fn:move','fn:integrate',inner,move)
        previous_flow='flow:initial' if number==1 else 'flow:next-1'
        velocity=port('fn:main','ctx:main',f'velocity#{number} = {speed}','out',f'velocity-{number}')
        dt=port('fn:main','ctx:main',f'dt#{number} = 0.5','out',f'dt-{number}')
        flow(f'flow:velocity-{number}',f'速度 · 调用 {number}',velocity,f'D{2+(number-1)*3}',f'literal-velocity@call{number}',[],ev)
        flow(f'flow:dt-{number}',f'时间 · 调用 {number}',dt,f'D{3+(number-1)*3}',f'literal-dt@call{number}',[],ev)
        for label,caller,flow_id in [('position',position,previous_flow),('velocity',velocity,f'flow:velocity-{number}'),('dt',dt,f'flow:dt-{number}')]:
            receiving=port('fn:move',ctx,label,'in',label)
            forwarding=port('fn:move',ctx,label,'out',label)
            formal=port('fn:integrate',inner,label,'in',label)
            edge(caller,receiving,flow_id,ctx,ev,'read' if label=='position' else 'data')
            edge(forwarding,formal,flow_id,inner,move)
        result=port('fn:integrate',inner,'position + velocity * dt','out','result')
        f=f'flow:next-{number}'
        flow(f,f'新位置 · 调用 {number}',result,f'D{4+(number-1)*3}',f'state#main.position@after-call{number}',[previous_flow,f'flow:velocity-{number}',f'flow:dt-{number}'],integrate)
        returned=port('fn:move',ctx,'next','in','next')
        writing=port('fn:move',ctx,'写回 state.position','out','written-position')
        written=port('fn:main','ctx:main',f'state.position@{number}','in',f'returned-{number}')
        edge(result,returned,f,inner,move)
        edge(writing,written,f,ctx,ev,'write')
        position=port('fn:main','ctx:main',f'state.position@{number}','out',f'position-{number}')
    # assert is a macro reference, not a fictitious ordinary call.
    code_edge('fn:main','external:assert','ctx:main',assertion,'reference')
    assertion_in=port('external:assert','ctx:main','state.position == 4.0f','in','condition-position')
    edge(position,assertion_in,'flow:next-2','ctx:main',assertion,'read')
    for table in ('entities','evidence','memberships','contexts','ports','flows','relations'):
        g[table] = list({row['id']:row for row in g[table]}.values())
    return g


def replay(store, *, existing_worker=None):
    initial=store.read();review=reviewed_graph(initial)
    for s in initial['sources']:
        read_source(initial,initial['project']['source_root'],s['id'])
    while True:
        current=store.read()
        running=[t for t in current['tasks'] if t['state']=='running']
        if running:
            require(len(running)==1 and existing_worker and running[0]['lease']['worker']==existing_worker and running[0]['lease']['expires_at']>time.time(), 'an existing reader still owns a task')
            t=running[0]
            batch={'format_version':'0.1','project_id':current['project']['id'],'snapshot_id':current['project']['snapshot_id'],'base_revision':current['project']['revision'],'batch_id':'batch:'+str(uuid4()),'task_id':t['id'],'lease_id':t['lease']['id'],'upserts':{},'new_tasks':[],'result':'done','reason':''}
        else:
            claimed=claim_next(store,'sample-review-replay')
            if claimed['state']=='no_ready_task':break
            t=claimed['task'];batch=claimed['batch_template']
        if t['kind']=='analyze':
            ids=set(t['source_ids'])
            symbols=[e for e in review['entities'] if ids.intersection(e['source_ids'])]
            symbols_ids={e['id'] for e in symbols}
            upserts={'sources':[s for s in review['sources'] if s['id'] in ids], 'entities':symbols,
                     'evidence':[e for e in review['evidence'] if e['source_id'] in ids],
                     'memberships':[m for m in review['memberships'] if m['axis']=='semantic' and m['child_id'] in symbols_ids]}
        else:
            upserts={'entities':[e for e in review['entities'] if not e['source_ids']], **{key:review[key] for key in ('contexts','ports','flows','relations')}}
        batch.update(upserts=upserts,result='done',reason='提交针对固定源码样本逐行核对的成果；这是既有阅读结果重放，不是通用自动分析。')
        commit_result(store,batch)
    return completion_report(store.read())


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--continue-worker')
    parser.add_argument('--export',type=Path)
    args=parser.parse_args()
    store=open_project(args.output) if args.continue_worker else initialize_project(BASE/'sample_repo',args.output)
    print(json.dumps(replay(store,existing_worker=args.continue_worker),ensure_ascii=False,indent=2))
    if args.export:export_graph(store,args.export)


if __name__=='__main__':main()
