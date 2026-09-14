"""Acceptance and adversarial regressions for the persistent skeleton protocol."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codemap.project import initialize_project
from codemap import skeleton as sk
from codemap import executor as ex
from codemap.agent import AgentTools
from codemap.core import ProtocolError
from codemap.syntax import parse


class SkeletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'repo'
        self.root.mkdir()
        self.code='def leaf(x=1):\n    return x + 1\n\ndef caller():\n    return leaf()\n\ndef outer():\n    return caller()\n'
        self.file=self.root/'a.py'
        self.file.write_text(self.code,encoding='utf-8')
        self.store=initialize_project(self.root,budget=100000)

    def node(self,name):
        return sk.find_symbol(self.store,name)['symbols'][0]

    def result(self,t):
        return {k:t[k] for k in ('node_id','body_sha','lease_id')} | {
            'summary':'Computes '+t['signature'], 'detail':{'role':'test'},'model':'host-test','used_tokens':100,
            'evidence':[{'node_id':t['node_id'],'body_sha':t['body_sha'],'start_line':t['start_line'],'end_line':t['end_line']}]}

    def review_all(self):
        tasks=ex.claim(self.store,'host',n=20)['tasks']
        ex.submit(self.store,{'batch_id':'all','results':[self.result(t) for t in tasks]})

    def test_repeat_is_deterministic_and_queries_are_read_only(self):
        def rows():
            with sk.connection(self.store) as db:
                return {t:[tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY 1,2')] for t in ('files','nodes','edges','search')}
        before=rows()
        with patch('codemap.skeleton.parse',side_effect=AssertionError('unchanged file parsed')):
            report=sk.sync(self.store)
        self.assertEqual(report['parsed_files'],0)
        self.assertEqual(before,rows())
        graph=self.store.read()
        self.assertEqual(sk.calls(self.store,'leaf')['edges'][0]['confidence'],'resolved_local')
        self.assertEqual(len(sk.calls(self.store,'leaf',depth=3)['edges']),2)
        self.assertEqual(len(sk.calls(self.store,'outer',direction='callees',depth=3)['edges']),2)
        self.assertEqual(sk.search(self.store,'leaf')['symbols'][0]['name'],'leaf')
        self.assertTrue(sk.repo_map(self.store,budget_tokens=10)['token_upper_bound']<=10)
        self.assertEqual(graph,self.store.read())
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_implementation_only_invalidates_changed_node(self):
        self.review_all()
        self.file.write_text(self.code.replace('x + 1','x + 2'),encoding='utf-8')
        r=sk.sync(self.store)
        self.assertEqual(r['changed'],[self.node('leaf')['id']])
        self.assertEqual(r['suspect'],[])
        self.assertEqual(self.node('leaf')['semantics']['status'],'stale')
        self.assertEqual(self.node('caller')['semantics']['status'],'current')
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_contract_propagates_one_hop_only(self):
        self.review_all()
        self.file.write_text(self.code.replace('x=1','x=2'),encoding='utf-8')
        r=sk.sync(self.store)
        self.assertEqual(r['suspect'],[self.node('caller')['id']])
        self.assertEqual(self.node('caller')['semantics']['status'],'suspect')
        self.assertEqual(self.node('outer')['semantics']['status'],'current')
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_format_and_rename_preserve_semantics_and_ids(self):
        self.review_all()
        before=self.node('leaf')
        self.file.write_text('\n\n'+self.code.replace('x + 1','x+1 # formatting'),encoding='utf-8')
        r=sk.sync(self.store)
        self.assertEqual(r['changed'],[])
        after=self.node('leaf')
        self.assertEqual(before['id'],after['id'])
        self.assertNotEqual(before['body_sha'],after['body_sha'])
        self.assertEqual(after['semantics']['status'],'current')
        self.assertEqual(after['semantics']['evidence'][0]['body_sha'],after['body_sha'])
        self.file.rename(self.root/'moved.py')
        r=sk.sync(self.store)
        self.assertEqual(r['changed'],[])
        self.assertEqual(len(r['renames']),1)
        self.assertEqual(self.node('leaf')['id'],before['id'])
        self.assertEqual(self.node('leaf')['path'],'moved.py')
        (self.root/'moved.py').write_text(self.code.replace('x + 1','x + 3'),encoding='utf-8')
        sk.sync(self.store)
        self.assertEqual(self.node('leaf')['id'],before['id'])
        self.assertEqual(self.node('leaf')['semantics']['status'],'stale')
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_budget_reservations_concurrent_claims_and_replay(self):
        sk.sync(self.store,budget=1)
        self.assertEqual(ex.claim(self.store,'host')['tasks'],[])
        sk.sync(self.store,budget=10000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda worker:ex.claim(self.store,worker),['one','two']))
        tasks=[r['tasks'][0] for r in results]
        self.assertNotEqual(tasks[0]['node_id'],tasks[1]['node_id'])
        batch={'batch_id':'replay','results':[self.result(tasks[0])]}
        ex.submit(self.store,batch)
        self.assertTrue(ex.submit(self.store,batch)['reused'])
        batch['results'][0]['summary']='different'
        with self.assertRaises(ProtocolError):ex.submit(self.store,batch)
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_old_lease_dirty_source_bad_evidence_and_atomic_failure(self):
        tasks=ex.claim(self.store,'host',n=3,now=10,lease_seconds=100)['tasks']
        results=[self.result(t) for t in tasks]
        results[-1]['body_sha']='wrong'
        with self.assertRaises(ProtocolError):ex.submit(self.store,{'batch_id':'bad','results':results},now=11)
        with sk.connection(self.store) as db:self.assertEqual(db.execute('SELECT count(*) FROM semantics').fetchone()[0],0)
        results=[self.result(t) for t in tasks]
        self.file.write_text(self.code+'\n# dirty',encoding='utf-8')
        with self.assertRaises(ProtocolError):ex.submit(self.store,{'batch_id':'dirty','results':results},now=11)
        sk.sync(self.store,budget=10000)
        with self.assertRaises(ProtocolError):ex.submit(self.store,{'batch_id':'expired','results':results},now=111)

    def test_mcp_host_roundtrip_without_codex_and_summary_fts(self):
        tools=AgentTools()
        self.addCleanup(tools.close)
        args={'project':str(Path(self.store.path).parent)}
        tasks=tools.call('blueprint_semantic_claim',args|{'worker':'arbitrary-host','n':3})['tasks']
        results=[self.result(t) for t in tasks]
        results[0]['summary']='raresemanticword'
        tools.call('blueprint_semantic_submit',args|{'batch':{'batch_id':'mcp','results':results}})
        found=tools.call('blueprint_search',args|{'query':'raresemanticword'})['symbols']
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['semantics']['status'],'current')
        for name,params in [('find_symbol',{'name':'leaf'}),('callers',{'symbol':'leaf'}),('callees',{'symbol':'outer'}),('repo_map',{'budget_tokens':200}),('doctor',{})]:
            self.assertIsInstance(tools.call('blueprint_'+name,args|params),dict)

    def test_resolution_scopes_ambiguity_unknown_and_cycles(self):
        self.file.write_text('def same():\n return same()\ndef outer():\n def same():\n  return 1\n return same()\n',encoding='utf-8')
        (self.root/'b.py').write_text('def duplicate():\n return 1\n')
        (self.root/'c.py').write_text('def duplicate():\n return 2\n')
        (self.root/'d.py').write_text('def use():\n duplicate()\n external()\n obj.same()\n')
        sk.sync(self.store)
        outer=self.node('outer')
        edges=sk.calls(self.store,outer['id'],direction='callees')['edges']
        self.assertEqual(edges[0]['dst_id'],self.node('outer.same')['id'])
        edges=sk.calls(self.store,'use',direction='callees')['edges']
        self.assertEqual(sum(e['confidence']=='ambiguous' for e in edges),2)
        self.assertEqual(sum(e['confidence']=='unresolved' for e in edges),2)
        same=[r for r in sk.find_symbol(self.store,'same')['symbols'] if r['qualified_name']=='same'][0]
        self.assertEqual(len(sk.calls(self.store,same['id'],depth=3)['edges']),1)
        recursive=sk.calls(self.store,same['id'],direction='callees')['edges']
        self.assertEqual(len(recursive),1)
        self.assertEqual(recursive[0]['confidence'],'resolved_local')
        with self.assertRaises(ProtocolError):sk.calls(self.store,'duplicate')
        with self.assertRaises(ProtocolError):sk.calls(self.store,'outer',depth=4)

    def test_filters_deletion_parser_change_and_doctor_corruption(self):
        (self.root/'b.py').write_text('def spare():\n return 1\n')
        sk.sync(self.store,include=['a.py'])
        self.assertFalse(sk.find_symbol(self.store,'spare')['symbols'])
        with sk.connection(self.store,write=True) as db:db.execute("UPDATE files SET parser_id='obsolete'")
        self.assertEqual(sk.sync(self.store)['parsed_files'],1)
        with sk.connection(self.store,write=True) as db:db.execute('DELETE FROM search')
        self.assertFalse(sk.doctor(self.store)['ok'])
        sk.sync(self.store)
        self.file.unlink()
        sk.sync(self.store)
        self.assertEqual(sk.doctor(self.store)['nodes'],0)




    def test_reusing_old_path_after_rename_does_not_merge_file_identity(self):
        self.review_all()
        before=self.node('leaf')
        self.file.rename(self.root/'moved.py')
        sk.sync(self.store)
        self.file.write_text(self.code,encoding='utf-8')
        sk.sync(self.store)
        found=sk.find_symbol(self.store,'leaf')['symbols']
        self.assertEqual(len(found),2)
        self.assertEqual(len({n['id'] for n in found}),2)
        moved=next(n for n in found if n['path']=='moved.py')
        fresh=next(n for n in found if n['path']=='a.py')
        self.assertEqual(moved['id'],before['id'])
        self.assertEqual(moved['semantics']['status'],'current')
        self.assertIsNone(fresh['semantics'])
        self.file.write_text(self.code.replace('x + 1','x + 8'),encoding='utf-8')
        sk.sync(self.store)
        self.assertEqual(sk.find_symbol(self.store,fresh['id'])['symbols'][0]['path'],'a.py')
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_more_than_100_files_and_unavailable_source_are_reported(self):
        for i in range(105):
            (self.root/f'unit_{i}.py').write_text(f'def value_{i}():\n return {i}\n')
        (self.root/'binary.py').write_bytes(b'\x00\x01')
        report=sk.sync(self.store)
        self.assertEqual(report['files'],107)
        self.assertEqual(report['nodes'],108)
        self.assertEqual(report['unavailable_files'],1)
        self.assertTrue(sk.find_symbol(self.store,'value_104')['symbols'])
        self.assertEqual(sk.summary(self.store)['file_states']['unavailable'],1)
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_new_resolution_candidate_marks_caller_suspect(self):
        (self.root/'b.py').write_text('def remote():\n return external()\n')
        sk.sync(self.store,budget=100000)
        self.review_all()
        (self.root/'c.py').write_text('def external():\n return 1\n')
        report=sk.sync(self.store)
        self.assertIn(self.node('remote')['id'],report['suspect'])
        self.assertEqual(self.node('remote')['semantics']['status'],'suspect')
        self.assertEqual(self.node('leaf')['semantics']['status'],'current')

    def test_cli_reopen_and_doctor_failure_exit(self):
        project=str(Path(self.store.path).parent)
        command=[sys.executable,'-B','-m','codemap']
        reopened=subprocess.run(command+['init',str(self.root)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(reopened.returncode,0,reopened.stderr)
        self.assertEqual(json.loads(reopened.stdout)['skeleton']['nodes'],3)
        with sk.connection(self.store,write=True) as db:db.execute('DELETE FROM search')
        broken=subprocess.run(command+['doctor',project],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(broken.returncode,1,broken.stderr)
        self.assertFalse(json.loads(broken.stdout)['ok'])

    def test_native_formatting_and_exact_spans(self):
        samples = {
            'cpp': ('native.cpp', 'int native(int x) { return x+1; }', 'int native( int x ) {\n return x + 1;\n}'),
            'csharp': ('native.cs', 'class Native { int Add(int x) { return x+1; } }', 'class Native {\n int Add( int x ) {\n return x + 1;\n }\n}'),
            'javascript': ('native.js', 'function native(x) { return x+1; }', 'function native( x ) {\n return x + 1;\n}'),
            'typescript': ('native.ts', 'function native(x: number) { return x+1; }', 'function native( x: number ) {\n return x + 1;\n}'),
        }
        for language,(path,before,after) in samples.items():
            with self.subTest(language=language):
                target=self.root/path
                target.write_text(before,encoding='utf-8')
                sk.sync(self.store,budget=100000)
                with sk.connection(self.store) as db:
                    nodes=[dict(r) for r in db.execute('SELECT n.* FROM nodes n JOIN files f ON n.file_id=f.id WHERE f.path=?',(path,))]
                self.assertTrue(nodes)
                old={n['id'] for n in nodes}
                for n in nodes:
                    document=json.loads(n['document'])
                    self.assertEqual(n['body_sha'],sha256(before.encode()[document['start_byte']:document['end_byte']]).hexdigest())
                target.write_text(after,encoding='utf-8')
                report=sk.sync(self.store)
                self.assertEqual(report['changed'],[])
                with sk.connection(self.store) as db:
                    new={r[0] for r in db.execute('SELECT n.id FROM nodes n JOIN files f ON n.file_id=f.id WHERE f.path=?',(path,))}
                self.assertEqual(old,new)

    def test_import_change_invalidates_local_meaning_and_callers(self):
        self.review_all()
        (self.root/'b.py').write_text('def remote():\n return outer()\n')
        sk.sync(self.store,budget=100000)
        self.file.write_text('import replacement\n'+self.code,encoding='utf-8')
        report=sk.sync(self.store)
        self.assertEqual(set(report['changed']),{self.node(n)['id'] for n in ('leaf','caller','outer')})
        self.assertIn(self.node('remote')['id'],report['suspect'])

    def test_lease_recovery_and_evidence_validation(self):
        task=ex.claim(self.store,'first',now=10,lease_seconds=1)['tasks'][0]
        reclaimed=ex.claim(self.store,'second',now=12)['tasks'][0]
        self.assertEqual(task['node_id'],reclaimed['node_id'])
        self.assertNotEqual(task['lease_id'],reclaimed['lease_id'])
        with self.assertRaises(ProtocolError):
            ex.submit(self.store,{'batch_id':'old','results':[self.result(task)]},now=13)
        wrong=self.result(reclaimed)
        wrong['evidence'][0]['start_line']=0
        with self.assertRaises(ProtocolError):
            ex.submit(self.store,{'batch_id':'evidence','results':[wrong]},now=13)
        ex.submit(self.store,{'batch_id':'recovered','results':[self.result(reclaimed)]},now=13)
        self.assertTrue(sk.doctor(self.store)['ok'])

    def test_exact_utf8_spans(self):
        code='@decorator\ndef café(x="你"):\n    return x\n'
        source={'id':'source:test','path':'u.py','language':'python','sha256':sha256(code.encode()).hexdigest()}
        n=parse(source,code)['symbols'][0]
        self.assertEqual(n['body_sha'],sha256(code.encode()[n['start_byte']:n['end_byte']]).hexdigest())


if __name__=='__main__':unittest.main()
