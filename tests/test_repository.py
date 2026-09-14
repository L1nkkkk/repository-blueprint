from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from codemap.core import ProtocolError, completion_report
from codemap.project import initialize_project, open_project, claim_next, commit_result, snapshot_status, canvas_graph
from codemap.repository import scan_repository, read_source, search_sources, identity, MAX_TEXT_BYTES
from codemap.server import create_server
from examples.review_sample import replay, BASE


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '中文 repository'
        self.root.mkdir()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        return target

    def test_inventory_includes_ignored_tests_tools_vendor_and_five_languages(self):
        files = {'.gitignore': 'private/\n', 'src/a.cpp': 'int unused() { return 1; }',
                 'private/a.py': 'def hidden(): pass', 'tests/b.cs': 'class Test {}',
                 'tools/c.ts': 'export const c=1;', 'vendor/d.js': 'const d=2;', 'README.md': '# Repository'}
        for path, content in files.items():
            self.write(path, content)
        self.write('.git/HEAD', 'ref: refs/heads/main')
        graph = scan_repository(self.root)
        self.assertEqual({s['path'] for s in graph['sources']}, set(files))
        self.assertTrue(graph['project']['inventory_complete'])
        self.assertEqual({s['language'] for s in graph['sources'] if s['category']=='source'}, {'cpp','python','csharp','typescript','javascript'})
        self.assertEqual(completion_report(graph)['sources_read'], 0)
        self.assertEqual(len([t for t in graph['tasks'] if t['kind']=='analyze']), len(files))
        roles = {e['name']:e['roles'] for e in graph['entities']}
        self.assertIn('test',roles['b.cs']); self.assertIn('tool',roles['c.ts']); self.assertIn('vendor',roles['d.js'])
        self.assertEqual(graph['inventory']['excluded_paths'][0]['path'],'.git')

    def test_explicit_exclusions_are_visible_and_ids_are_repeatable(self):
        self.write('src/a.py','x=1');self.write('cache/file.py','x=2')
        first = scan_repository(self.root,excludes=['cache'])
        second = scan_repository(self.root,excludes=['cache'])
        self.assertEqual(first,second)
        self.assertEqual([s['path'] for s in first['sources']],['src/a.py'])
        self.assertIn('cache',str(first['inventory']['excluded_paths']))

    def test_unreadable_directory_is_a_gap_and_prevents_completion(self):
        self.write('src/a.py','x=1')
        import os
        real = os.scandir
        def fail(path):
            if Path(path).name=='src':raise PermissionError('unreadable directory')
            return real(path)
        with patch('codemap.repository.os.scandir',side_effect=fail):graph=scan_repository(self.root)
        self.assertFalse(graph['project']['inventory_complete'])
        self.assertEqual(completion_report(graph)['status'],'inventory_incomplete')
        self.assertTrue(any(t['state']=='blocked' for t in graph['tasks']))

    def test_failed_file_hash_is_not_fabricated(self):
        self.write('broken.py','x=1')
        with patch('codemap.repository.fingerprint_file',side_effect=PermissionError('cannot read')):graph=scan_repository(self.root)
        self.assertEqual(graph['sources'],[])
        self.assertEqual(graph['inventory']['gaps'][0]['path'],'broken.py')

    def test_a_directory_changed_during_scan_is_not_claimed_complete(self):
        self.write('a.py','x=1')
        from codemap.repository import fingerprint_file
        def mutate(path):
            result=fingerprint_file(path)
            self.write('new.py','x=2')
            import os
            stamp=self.root.stat().st_mtime_ns
            os.utime(self.root,ns=(stamp,stamp+1000000000))
            return result
        with patch('codemap.repository.fingerprint_file',side_effect=mutate):graph=scan_repository(self.root)
        self.assertFalse(graph['project']['inventory_complete'])
        self.assertIn('changed during scan',graph['inventory']['gaps'][0]['reason'])

    def test_symlink_target_is_not_followed(self):
        outside=Path(self.temp.name)/'outside.py';outside.write_text('secret=1')
        try:(self.root/'link.py').symlink_to(outside)
        except OSError as error:self.skipTest(f'Symlink creation unavailable: {error}')
        graph=scan_repository(self.root)
        self.assertEqual(graph['sources'],[])
        self.assertFalse(graph['project']['inventory_complete'])

    def test_large_code_and_nontext_source_remain_blocked_not_excluded(self):
        self.write('big.cpp',b'x'*(MAX_TEXT_BYTES+1));self.write('broken.py',b'\x00\x01');self.write('image.bin',b'\x00\x01')
        graph=scan_repository(self.root)
        sources={s['path']:s for s in graph['sources']}
        self.assertTrue(sources['big.cpp']['included']);self.assertEqual(sources['broken.py']['read_state'],'failed')
        self.assertFalse(sources['image.bin']['included'])
        self.assertEqual(len([t for t in graph['tasks'] if t['state']=='blocked']),2)
        with self.assertRaisesRegex(ProtocolError,'limit'):read_source(graph,self.root,'big.cpp')

    def test_numbered_bounded_reading_does_not_claim_analysis_progress(self):
        self.write('你好.py','甲\n乙\n丙\n')
        graph=scan_repository(self.root)
        page=read_source(graph,self.root,'你好.py',start=2,limit=1)
        self.assertEqual(page['lines'],[{'number':2,'text':'乙'}]);self.assertEqual(page['next_start'],3)
        self.assertEqual(graph['sources'][0]['read_state'],'unread')
        for start,limit in [(0,1),(1,501),(9,1)]:
            with self.assertRaises(ProtocolError):read_source(graph,self.root,'你好.py',start=start,limit=limit)
        with self.assertRaisesRegex(ProtocolError,'inventory'):read_source(graph,self.root,'../outside.py')

    def test_utf16_bom_text_is_read_with_correct_lines(self):
        self.write('wide.cs','class 中文 {}\n// 你好'.encode('utf-16'))
        graph=scan_repository(self.root)
        self.assertEqual(read_source(graph,self.root,'wide.cs')['lines'][1]['text'],'// 你好')

    def test_empty_source_has_one_logical_line_for_review_evidence(self):
        self.write('empty.py','')
        store=initialize_project(self.root)
        current=store.read()
        page=read_source(current,self.root,'empty.py')
        self.assertTrue(page['empty_file']);self.assertEqual(page['lines'],[{'number':1,'text':''}])
        claimed=claim_next(store,'reader',now=100)
        s=deepcopy(claimed['sources'][0]);s.update(read_state='read',symbols_complete=True)
        e=deepcopy(claimed['entities'][0]);e.update(analysis='reviewed',summary='Empty source file; no declarations.',evidence_ids=['ev:empty'])
        proof={'id':'ev:empty','source_id':s['id'],'sha256':s['sha256'],'start_line':1,'end_line':1,'note':'Empty file at this fingerprint'}
        batch=claimed['batch_template'];batch.update(upserts={'sources':[s],'entities':[e],'evidence':[proof]},result='done',reason='Empty file verified')
        commit_result(store,batch,now=101)
        from codemap.core import validate_graph
        validate_graph(store.read(),self.root)
        self.assertEqual(next(t for t in store.read()['tasks'] if t['kind']=='analyze')['state'],'done')

    def test_search_reports_truncation_and_stale_file_failure(self):
        target=self.write('a.py','needle\nNEEDLE\nneedle')
        self.write('b.py','needle')
        graph=scan_repository(self.root)
        result=search_sources(graph,self.root,'needle',limit=1)
        self.assertTrue(result['truncated']);self.assertEqual(len(result['matches']),1)
        target.write_text('changed')
        result=search_sources(graph,self.root,'needle')
        self.assertEqual(result['failures'][0]['path'],'a.py')

    def test_project_does_not_overwrite_and_excludes_its_own_output(self):
        self.write('a.py','x=1')
        store=initialize_project(self.root)
        self.assertEqual([s['path'] for s in store.read()['sources']],['a.py'])
        with self.assertRaisesRegex(ProtocolError,'never overwritten'):initialize_project(self.root)
        self.assertEqual(snapshot_status(store.read())['state'],'current')
        self.assertEqual(open_project(self.root/'.codemap').read(),store.read())

    def test_default_project_keeps_sources_on_disk_and_executor_reads_them(self):
        self.write('a.py', 'def answer():\n    return 42\n')
        store = initialize_project(self.root, budget=100000)
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_texts').fetchone()[0], 0)
        from codemap.executor import claim
        claimed = claim(store, 'disk-reader', n=1)
        self.assertEqual(claimed['state'], 'claimed')
        self.assertIn('return 42', claimed['tasks'][0]['source'])

    def test_snapshot_change_addition_deletion_are_reported(self):
        a=self.write('a.py','x=1');b=self.write('b.py','y=1')
        graph=scan_repository(self.root)
        a.write_text('x=2');b.unlink();self.write('c.py','z=1')
        result=snapshot_status(graph)
        self.assertEqual(result['changed'],['a.py']);self.assertEqual(result['deleted'],['b.py']);self.assertEqual(result['added'],['c.py'])

    def test_bad_or_stale_evidence_commit_leaves_task_and_graph_unchanged(self):
        source=self.write('a.py','x=1\n')
        store=initialize_project(self.root)
        claimed=claim_next(store,'actual-reader',now=100)
        batch=claimed['batch_template']
        record=deepcopy(claimed['entities'][0]);record.update(analysis='reviewed',summary='Assign x.',evidence_ids=['ev:a'])
        s=deepcopy(claimed['sources'][0]);s.update(read_state='read',symbols_complete=True)
        proof={'id':'ev:a','source_id':s['id'],'sha256':s['sha256'],'start_line':1,'end_line':9,'note':'Assignment'}
        batch.update(upserts={'entities':[record],'sources':[s],'evidence':[proof]},result='done',reason='Reviewed assignment')
        previous=store.read()
        with self.assertRaisesRegex(ProtocolError,'exceeds file'):commit_result(store,batch,now=101)
        self.assertEqual(store.read(),previous)
        proof['end_line']=1;source.write_text('x=2\n')
        with self.assertRaisesRegex(ProtocolError,'snapshot changed'):commit_result(store,batch,now=101)
        self.assertEqual(store.read(),previous)

    def test_renew_pause_recover_and_retry_preserve_task_state(self):
        self.write('a.py','x=1')
        store=initialize_project(self.root)
        claimed=claim_next(store,'reader',now=100,lease_seconds=10);t=claimed['task']
        renewed=store.renew(t['id'],t['lease']['id'],expected_revision=1,now=105,lease_seconds=20)
        self.assertEqual(next(t for t in renewed['tasks'] if t['state']=='running')['lease']['expires_at'],125)
        store.set_paused(True,expected_revision=2)
        with self.assertRaisesRegex(ProtocolError,'paused'):claim_next(store,'reader2',now=126)
        store.set_paused(False,expected_revision=4)
        again=claim_next(store,'reader2',now=130)
        self.assertEqual(again['task']['attempt'],2)
        batch=again['batch_template'];batch.update(result='blocked',reason='Need a missing declaration')
        commit_result(store,batch,now=131)
        store.retry(t['id'],expected_revision=store.read()['project']['revision'])
        self.assertEqual(next(t for t in store.read()['tasks'] if t['kind']=='analyze')['state'],'queued')


class WholeSampleTests(unittest.TestCase):
    def test_real_inventory_review_and_reopen_cover_all_files_and_call_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=initialize_project(BASE/'sample_repo',Path(tmp)/'map')
            store.save_view('canvas',{'theme':'dark','layouts':{'test':{'positions':{'fn:move':{'x':34,'y':90,'locked':True}}}}})
            report=replay(store)
            self.assertEqual(report['status'],'complete')
            graph=open_project(Path(tmp)/'map').read()
            self.assertEqual(len(graph['sources']),5)
            self.assertTrue(all(s['read_state']=='read' for s in graph['sources']))
            self.assertEqual({e['name'] for e in graph['entities'] if e['kind'] in {'function','method'}},{'Measure','DisplayUnits','IntegratePosition','Move','main'})
            self.assertEqual({c['id'] for c in graph['contexts'] if c.get('callee_id')=='fn:move'},{'ctx:move-1','ctx:move-2'})
            flow=next(f for f in graph['flows'] if f['id']=='flow:next-2')
            self.assertIn('flow:next-1',flow['derived_from']);self.assertNotIn('flow:velocity-1',flow['derived_from'])
            self.assertEqual(store.read_view('canvas')['theme'],'dark')
            self.assertEqual(snapshot_status(graph)['state'],'current')

    def test_review_replay_rejects_other_source_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'src';root.mkdir();(root/'different.cpp').write_text('int main(){}')
            store=initialize_project(root)
            with self.assertRaisesRegex(ProtocolError,'does not match'):replay(store)


class CanvasServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)/'repo';root.mkdir();(root/'a.py').write_text('x=1\n')
        self.store=initialize_project(root)
        self.server=create_server(self.store)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,method='GET',body=None,headers=None):
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        connection.request(method,path,body,headers or {})
        response=connection.getresponse();content=response.read();connection.close()
        return response.status,content

    def test_local_canvas_graph_source_and_saved_theme_are_real(self):
        code,body=self.request('/');self.assertEqual(code,200)
        self.assertIn(b'/canvas-model.js',body)
        self.assertIn(b'/workflow-model.js',body)
        code,workflow=self.request('/workflow-model.js');self.assertEqual(code,200)
        self.assertIn(b'function project',workflow)
        code,body=self.request('/canvas-model.js');self.assertEqual(code,200)
        self.assertIn(b'scopedData',body)
        code,body=self.request('/api/graph');self.assertEqual(code,200)
        response=json.loads(body);self.assertEqual(response['graph']['sources'][0]['path'],'a.py')
        source_id=response['graph']['sources'][0]['id']
        code,body=self.request('/api/source?id='+source_id)
        self.assertEqual(json.loads(body)['lines'][0]['text'],'x=1')
        headers={'Content-Type':'application/json','X-Codemap-Token':response['write_token']}
        code,_=self.request('/api/view','POST',json.dumps({'theme':'light','layouts':{'test':{'x':20}}}),headers)
        self.assertEqual(code,200);self.assertEqual(self.store.read_view('canvas')['theme'],'light')
        self.assertEqual(self.store.read()['project']['revision'],0)

    def test_source_traversal_cross_origin_and_uncredentialed_write_are_rejected(self):
        self.assertEqual(self.request('/api/source?id=../outside')[0],400)
        self.assertEqual(self.request('/api/graph',headers={'Origin':'https://outside.example'})[0],400)
        self.assertEqual(self.request('/api/graph',headers={'Host':'outside.example'})[0],400)
        self.assertEqual(self.request('/api/view','POST','{}',{'Content-Type':'application/json'})[0],400)
        self.assertIsNone(self.store.read_view('canvas'))

    def test_source_highlighting_context_is_optional_snapshot_checked_and_locally_served(self):
        root=Path(self.store.read()['project']['source_root'])
        (root/'a.py').write_text('doc = """start\nreturn is text\nend"""\nreturn 4\n')
        graph=scan_repository(root)
        page=read_source(graph,root,'a.py',start=2,limit=2,with_context=True)
        self.assertEqual(page['language'],'python')
        self.assertEqual(page['highlight_prefix'],'doc = """start\n')
        self.assertEqual([line['text'] for line in page['lines']],['return is text','end"""'])
        self.assertNotIn('highlight_prefix',read_source(graph,root,'a.py',start=2,limit=2))
        # The HTTP reader must reject the now-stale saved snapshot even for highlighting.
        self.assertEqual(self.request('/api/source?id=a.py&start=2&context=1')[0],400)
        for path in ('/panel-layout.js','/camera.js','/source-highlight.js','/vendor/highlight.min.js','/reader.css'):
            code,body=self.request(path);self.assertEqual(code,200,path);self.assertTrue(body)
        self.assertEqual(self.request('/vendor/../server.py')[0],404)

    def test_workbench_endpoints_are_real_and_mutations_require_local_write_token(self):
        for path in ('/workbench.js','/workbench.css','/api/coverage','/api/requests','/api/history'):
            self.assertEqual(self.request(path)[0],200,path)
        coverage=json.loads(self.request('/api/coverage')[1])
        self.assertEqual(coverage['reading']['sources_read'],0)
        self.assertEqual(coverage['scenario_count'],0)
        self.assertEqual(json.loads(self.request('/api/requests')[1])['requests'],[])
        for path in ('/api/requests','/api/changes/preview','/api/history/restore'):
            self.assertEqual(self.request(path,'POST','{}',{'Content-Type':'application/json'})[0],400,path)
        token=json.loads(self.request('/api/graph')[1])['write_token']
        headers={'Content-Type':'application/json','X-Codemap-Token':token}
        self.assertEqual(self.request('/api/changes/preview','POST',json.dumps({'renames':[]}),headers)[0],200)
        self.assertEqual(self.request('/api/requests','POST',json.dumps({'action':'create','request':{'id':'','kind':'question','question':'test'}}),headers)[0],400)
        self.assertEqual(json.loads(self.request('/api/requests')[1])['requests'],[])

    def test_changed_file_is_shown_as_stale_and_cannot_be_displayed_as_current(self):
        graph=self.store.read();Path(graph['project']['source_root'],'a.py').write_text('x=2\n')
        code,body=self.request('/api/graph');result=json.loads(body)
        self.assertEqual(result['snapshot']['state'],'changed')
        self.assertEqual(result['status']['coverage']['status'],'snapshot_changed')
        file=next(e for e in result['graph']['entities'] if e['kind']=='file')
        self.assertEqual(file['freshness'],'stale')
        self.assertEqual(self.request('/api/source?id=a.py')[0],400)

    def test_change_panel_preview_and_apply_preserve_layout_and_require_current_plan(self):
        code, body = self.request('/api/graph'); payload = json.loads(body)
        headers = {'Content-Type':'application/json', 'X-Codemap-Token':payload['write_token']}
        self.request('/api/view', 'POST', json.dumps({'theme':'dark', 'scope':'reader-position'}), headers)
        graph = self.store.read()
        Path(graph['project']['source_root'], 'a.py').write_text('x=2\n')
        code, body = self.request('/api/changes'); plan = json.loads(body)
        self.assertEqual(code, 200); self.assertEqual(plan['changed'], ['a.py'])
        request = json.dumps({'expected_revision':plan['base_revision'], 'plan_id':plan['plan_id']})
        self.assertEqual(self.request('/api/changes','POST',request,{'Content-Type':'application/json'})[0], 400)
        self.assertEqual(self.store.read(), graph)
        code, body = self.request('/api/changes','POST',request,headers)
        self.assertEqual(code, 200); self.assertEqual(json.loads(body)['coverage']['status'], 'partial')
        self.assertEqual(self.store.read_view('canvas'), {'theme':'dark', 'scope':'reader-position'})
        self.assertEqual(json.loads(self.request('/api/source?id=a.py')[1])['lines'][0]['text'], 'x=2')
        self.assertEqual(json.loads(self.request('/api/changes')[1])['state'], 'current')


if __name__=='__main__':unittest.main()
