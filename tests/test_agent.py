from copy import deepcopy
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

from codemap.agent import AgentTools
from codemap.core import ProtocolError
from codemap.mcp import StdioServer
from scripts.build_plugin import build


class Client:
    def __init__(self, entry, cwd, *, config=None, name='integration-test'):
        command = [config['command'], *config['args']] if config else [sys.executable, '-u', str(entry), 'mcp']
        self.process = subprocess.Popen(command, cwd=cwd, env={**os.environ, **(config or {}).get('env', {})},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        self.responses = queue.Queue()
        self.counter = 0
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()
        self.rpc('initialize', {'protocolVersion':'2025-11-25', 'capabilities':{},
                               'clientInfo':{'name':name,'version':'1'}})
        self.send({'jsonrpc':'2.0', 'method':'notifications/initialized'})

    def _read(self):
        for line in self.process.stdout:
            try: self.responses.put(json.loads(line))
            except ValueError: self.responses.put({'invalid_stdout':line})

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.counter += 1
        self.send({'jsonrpc':'2.0', 'id':self.counter, 'method':method, 'params':params})
        response = self.responses.get(timeout=8)
        if response.get('id') != self.counter or 'error' in response:
            raise AssertionError(response)
        return response['result']

    def call(self, name, **args):
        result = self.rpc('tools/call', {'name':'blueprint_' + name,'arguments':args})
        if result.get('isError'):
            raise AssertionError(result)
        return result['structuredContent']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


class AgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '中文 源码'
        self.root.mkdir()
        (self.root / '值.py').write_text('x = 1\n', encoding='utf-8')
        self.plugin = build(self.base / '移动后的 插件/repository-blueprint')
        self.entry = self.plugin / 'scripts/run.py'
        self.client = Client(self.entry, self.base)
        self.addCleanup(self.client.close)
        self.map = self.client.call('init', root=str(self.root))['project_directory']

    def test_relocated_package_exposes_tools_and_all_guides_without_checkout(self):
        tools = self.client.rpc('tools/list', {})['tools']
        self.assertEqual(len(tools), 25)
        self.assertTrue({'blueprint_find_symbol','blueprint_callers','blueprint_callees',
                         'blueprint_repo_map','blueprint_sync','blueprint_doctor',
                         'blueprint_semantic_claim','blueprint_semantic_submit'} <= {t['name'] for t in tools})
        index = self.client.call('index', project=self.map, source='值.py')
        self.assertEqual(index['records'][0]['name'], 'x')
        self.assertEqual(index['state'], 'parsed')
        for topic in ('workflow','commands','preparation','structure','reading-pack','graph-format','task-protocol','catalog'):
            guide = self.client.call('guide', topic=topic)
            self.assertTrue(guide.get('text') or guide.get('catalog'))
        page = self.client.call('guide', topic='graph-format', limit=1)
        self.assertEqual(page['next_start'], 2)
        self.assertEqual(self.client.call('status', project=self.map)['snapshot']['state'], 'current')
        self.assertTrue(self.client.call('init', root=str(self.root))['reopened'])
        self.assertFalse(any(p.name in {'map.sqlite','sample_repo','__pycache__'} for p in self.plugin.rglob('*')
                             if '__pycache__' not in str(p)))
        # The packaged guides must not send agents back to the developer checkout.
        import re
        for guide in (self.plugin / 'skills/repository-blueprint').rglob('*.md'):
            for target in re.findall(r'\]\(([^)]+)\)', guide.read_text(encoding='utf-8')):
                if '://' not in target and not target.startswith('#'):
                    self.assertTrue((guide.parent / target.split('#')[0]).is_file(), (guide, target))
        rebuilt = build(self.base / '再次移动/repository-blueprint', source=self.plugin)
        self.assertTrue((rebuilt / 'runtime/codemap/web/topology-model.js').is_file())
        self.assertEqual((rebuilt / 'skills/repository-blueprint/references/graph-format.md').read_bytes(),
                         (self.plugin / 'skills/repository-blueprint/references/graph-format.md').read_bytes())

    def test_metrics_are_available_through_packaged_stdio_http_and_cli(self):
        from urllib.parse import urlsplit
        self.client.call('read', project=self.map, source='值.py')
        address = urlsplit(self.client.call('canvas', project=self.map)['url'])
        data = self.client.call('metrics', project=self.map)
        self.assertEqual(data['selected']['client'], 'integration-test')
        self.assertTrue(data['snapshot_matches_start'])
        connection = HTTPConnection(address.hostname, address.port, timeout=3)
        connection.request('GET', '/api/metrics?session=' + data['selected']['id'])
        response = connection.getresponse(); self.assertEqual(response.status, 200)
        via_http = json.loads(response.read()); connection.close()
        self.assertEqual(via_http['summary']['calls'], data['summary']['calls'])
        self.assertEqual(via_http['summary']['tool_ms'], data['summary']['tool_ms'])
        cli = subprocess.run([sys.executable, str(self.entry), 'metrics', self.map, '--session', data['selected']['id']],
                             capture_output=True, encoding='utf-8', check=True, timeout=10)
        self.assertEqual(json.loads(cli.stdout)['summary']['calls'], data['summary']['calls'])

    def test_packaged_prepare_reports_fields_and_reuses_pack_reading_after_repair(self):
        claim = self.client.call('next', project=self.map, worker='external-agent')
        self.assertIn('function_details_template', claim['preparation_contract'])
        entity = claim['entities'][0]
        upserts = {'sources': [{'source': '值.py', 'read_state': 'read', 'symbols_complete': True}],
                   'evidence': [{'key': 'line', 'source': '值.py', 'start_line': 1, 'end_line': 1, 'note': 'Literal assignment.'}],
                   'entities': [{'id': entity['id'], 'summary': 'Defines x as 1.', 'evidence_ids': ['$line']}]}
        args = {'project': self.map, 'batch_template': claim['batch_template'], 'upserts': upserts,
                'result': 'done', 'reason': 'Entire one-line module checked.'}
        reply = self.client.rpc('tools/call', {'name': 'blueprint_prepare', 'arguments': args})
        self.assertTrue(reply['isError'])
        report = reply['structuredContent']
        self.assertEqual(json.loads(reply['content'][0]['text']), report)
        self.assertEqual(report['issues'][0]['field'], 'analysis')
        self.assertEqual(report['issues'][0]['record_id'], entity['id'])
        self.assertEqual(report['issues'][0]['source_paths'], ['值.py'])
        self.assertEqual(report['categories'], ['validation'])
        self.assertEqual(self.client.call('metrics', project=self.map)['summary']['errors'], 1)
        # Source was already delivered in the claim's reading pack; no extra read.
        upserts['entities'][0]['analysis'] = 'reviewed'
        draft = self.client.call('prepare', **args)
        committed = self.client.call('commit', project=self.map, prepared_id=draft['prepared_id'])
        self.assertEqual(committed['coverage']['sources_read'], 1)

    def test_read_commit_idempotency_invalid_evidence_and_cross_session_reopen(self):
        claimed = self.client.call('next', project=self.map, worker='test-reader', detail='full')
        source = self.client.call('read', project=self.map, source='值.py')
        self.assertEqual(source['lines'], [{'number':1, 'text':'x = 1'}])
        entity = deepcopy(claimed['entities'][0])
        entity.update(analysis='reviewed', summary='Module assigns the integer constant 1 to x; no function declarations.', evidence_ids=['ev:assignment'])
        record = deepcopy(claimed['sources'][0])
        record.update(read_state='read', symbols_complete=True)
        proof = {'id':'ev:assignment','source_id':record['id'],'sha256':record['sha256'],
                 'start_line':1,'end_line':50,'note':'Module assignment'}
        batch = claimed['batch_template']
        batch.update(upserts={'entities':[entity],'sources':[record],'evidence':[proof]},result='done',reason='Full one-line module read')
        bad = self.client.rpc('tools/call', {'name':'blueprint_commit','arguments':{'project':self.map,'batch':batch}})
        self.assertTrue(bad['isError'])
        self.assertEqual(self.client.call('status', project=self.map)['project']['revision'], claimed['project']['revision'])
        proof['end_line'] = 1
        saved = self.client.call('commit', project=self.map, batch=batch)
        again = self.client.call('commit', project=self.map, batch=batch)
        self.assertEqual(saved['project']['revision'], again['project']['revision'])
        self.client.close()
        reopened = Client(self.entry, self.base)
        self.addCleanup(reopened.close)
        report = reopened.call('status', project=self.map)
        self.assertEqual(report['coverage']['sources_read'], 1)
        self.assertEqual(report['task_counts']['queued'], 1)  # repository review still required
        self.assertNotEqual(report['coverage']['status'], 'complete')

    def test_portable_configs_start_stdio_readers_and_preserve_commits_across_clients(self):
        portable = build(self.base / '通用 发行/repository-blueprint', kind='portable')
        self.assertFalse((portable / '.codex-plugin').exists())
        self.assertFalse((portable / 'scripts/install_plugin.py').exists())
        self.assertFalse((portable / 'install.ps1').exists())
        entry = portable / 'scripts/run.py'
        configs = {client: json.loads((portable / f'connections/{client}.json').read_text(encoding='utf-8'))['mcpServers']['repository-blueprint'] for client in ('claude-code','gemini-cli')}
        for config in configs.values():
            self.assertNotIn('startup_timeout_sec', config)
            self.assertNotIn('tool_timeout_sec', config)
        first = Client(entry, self.base, config=configs['claude-code'], name='portable-reader-one')
        self.addCleanup(first.close)
        self.assertEqual(first.call('guide', topic='workflow')['topic'], 'workflow')
        claimed = first.call('next', project=self.map, worker='first-client', detail='full')
        proof_source = first.call('read', project=self.map, source='值.py')
        source = deepcopy(claimed['sources'][0]); source.update(read_state='read', symbols_complete=True)
        entity = deepcopy(claimed['entities'][0]); entity.update(analysis='reviewed', summary='Module assigns 1 to x.', evidence_ids=['proof:portable'])
        proof = {'id':'proof:portable','source_id':source['id'],'sha256':proof_source['sha256'],'start_line':1,'end_line':1,'note':'Literal assignment read through MCP.'}
        batch = claimed['batch_template']; batch.update(upserts={'sources':[source],'entities':[entity],'evidence':[proof]},result='done',reason='One-line module fully read.')
        first.call('commit', project=self.map, batch=batch)
        first.close()
        second = Client(entry, self.base, config=configs['gemini-cli'], name='portable-reader-two')
        self.addCleanup(second.close)
        self.assertEqual(second.call('status', project=self.map)['coverage']['sources_read'], 1)
        self.assertEqual(second.call('query', project=self.map, table='evidence', ids=['proof:portable'])['records'], [proof])
        following = second.call('next', project=self.map, worker='second-client')
        self.assertNotEqual(following['task']['id'], claimed['task']['id'])
        self.assertEqual(following['task']['lease']['worker'], 'second-client')
        rebuilt = build(self.base / '重新解压/repository-blueprint', source=portable, kind='portable')
        value = json.loads(subprocess.check_output([sys.executable, str(rebuilt / 'scripts/run.py'), 'connect', '--client', 'gemini-cli'], cwd=self.base, encoding='utf-8'))
        self.assertEqual(value['mcpServers']['repository-blueprint']['args'][1], str(rebuilt / 'scripts/run.py'))
        self.assertFalse((rebuilt / '.codex-plugin').exists())

    def test_prepared_batch_crosses_stdio_connections_without_resending_payload(self):
        claimed = self.client.call('next', project=self.map, worker='prepared-reader')
        self.client.call('read', project=self.map, source='值.py')
        draft = self.client.call('prepare', project=self.map, batch_template=claimed['batch_template'],
            upserts={'sources': [{'source': '值.py', 'read_state': 'read', 'symbols_complete': True}],
                     'evidence': [{'key': 'assignment', 'source': '值.py', 'start_line': 1, 'end_line': 1, 'note': 'Assigns 1 to x.'}],
                     'entities': [{'id': claimed['entities'][0]['id'], 'analysis': 'reviewed', 'summary': 'Assigns 1 to x; no function declarations.', 'evidence_ids': ['$assignment']}]},
            result='done', reason='Entire one-line module read.')
        self.assertNotIn('batch', draft)
        self.assertEqual(self.client.call('status', project=self.map)['project']['revision'], claimed['project']['revision'])
        self.client.close()
        other = Client(self.entry, self.base)
        self.addCleanup(other.close)
        compiled = other.call('prepare', project=self.map, prepared_id=draft['prepared_id'], detail='full')
        self.assertEqual(compiled['batch']['upserts']['evidence'][0]['id'], draft['aliases']['assignment'])
        saved = other.call('commit', project=self.map, prepared_id=draft['prepared_id'])
        retried = other.call('commit', project=self.map, prepared_id=draft['prepared_id'])
        self.assertEqual(saved['receipt'], retried['receipt'])
        context = other.call('context', project=self.map, source='值.py')
        self.assertEqual(context['records'][0]['reuse'], 'current_evidence')
        self.assertEqual(saved['task_counts']['queued'], 1)

    def test_external_handoff_carries_selection_without_starting_or_mutating_work(self):
        from urllib.parse import urlsplit
        canvas = self.client.call('canvas', project=self.map)
        parsed = urlsplit(canvas['url']); connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/graph'); before = json.loads(connection.getresponse().read())
        connection.request('GET', '/api/requests'); requests = json.loads(connection.getresponse().read())['requests']
        connection.request('GET', '/api/agent-connection?client=gemini-cli')
        info = json.loads(connection.getresponse().read()); self.assertEqual(info['config']['mcpServers']['repository-blueprint']['timeout'], 120000)
        graph = before['graph']; entity = next(e for e in graph['entities'] if e['kind'] == 'file')
        payload = {'client':'claude-code','kind':'question','question':'解释这个文件的输入。','entity_id':entity['id']}
        headers = {'Content-Type':'application/json','X-Codemap-Token':before['write_token']}
        connection.request('POST', '/api/handoff', json.dumps(payload), headers)
        response = connection.getresponse(); self.assertEqual(response.status, 200)
        handoff = json.loads(response.read()); self.assertFalse(handoff['started'])
        self.assertIn(self.map.replace('\\', '\\\\'), handoff['prompt'])
        self.assertIn(entity['id'], handoff['prompt'])
        self.assertEqual(handoff['config']['mcpServers']['repository-blueprint']['type'], 'stdio')
        connection.request('POST', '/api/handoff', json.dumps({**payload,'entity_id':'missing-node'}), headers)
        response = connection.getresponse(); self.assertEqual(response.status, 400); response.read()
        connection.request('POST', '/api/handoff', json.dumps({**payload,'client':[]}), headers)
        response = connection.getresponse(); self.assertEqual(response.status, 400); response.read()
        connection.request('GET', '/api/requests'); self.assertEqual(json.loads(connection.getresponse().read())['requests'], requests)
        connection.request('GET', '/api/graph'); self.assertEqual(json.loads(connection.getresponse().read())['graph'], graph)

    def test_canvas_reuses_endpoint_and_persists_layout_after_process_exit(self):
        canvas = self.client.call('canvas', project=self.map)
        self.assertEqual(canvas['url'], self.client.call('canvas', project=self.map)['url'])
        from urllib.parse import urlsplit
        parsed = urlsplit(canvas['url'])
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request('GET', '/api/graph')
        graph = json.loads(connection.getresponse().read())
        connection.request('POST','/api/view',json.dumps({'theme':'light','layouts':{'custom':{'x':12}}}),
                           {'Content-Type':'application/json','X-Codemap-Token':graph['write_token']})
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        self.client.close()
        # The MCP connection owns only its HTTP servers; stored view state remains.
        fresh = Client(self.entry, self.base)
        self.addCleanup(fresh.close)
        parsed = urlsplit(fresh.call('canvas', project=self.map)['url'])
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request('GET','/api/graph')
        self.assertEqual(json.loads(connection.getresponse().read())['view']['theme'], 'light')
        connection.close()

    def test_packaged_update_previews_applies_and_reads_new_snapshot_through_stdio(self):
        old = self.client.call('next', project=self.map, worker='before-update')
        (self.root/'值.py').write_text('x = 2\n', encoding='utf-8')
        plan = self.client.call('update', project=self.map)
        self.assertEqual(plan['changed'], ['值.py'])
        updated = self.client.call('update', project=self.map, action='apply', expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertNotEqual(old['project']['snapshot_id'], updated['project']['snapshot_id'])
        stale = self.client.rpc('tools/call', {'name':'blueprint_commit', 'arguments':{'project':self.map, 'batch':old['batch_template']}})
        self.assertTrue(stale['isError'])
        new = self.client.call('next', project=self.map, worker='after-update')
        self.assertEqual(new['task']['kind'], 'update')
        self.assertEqual(self.client.call('read', project=self.map, source='值.py')['lines'][0]['text'], 'x = 2')
        self.assertEqual(len(self.client.call('update', project=self.map, action='history')['snapshots']), 1)
        self.assertTrue(self.client.call('update', project=self.map, action='apply', expected_revision=plan['base_revision'], plan_id=plan['plan_id'])['reused'])

    def test_query_revision_filters_and_task_control(self):
        page = self.client.call('query', project=self.map, table='entities', limit=1)
        self.assertIsNotNone(page['next_offset'])
        first = page['records'][0]
        exact = self.client.call('query', project=self.map, table='entities', ids=[first['id']])
        self.assertEqual(exact['records'], [first])
        self.client.call('task', project=self.map, action='pause')
        stale = self.client.rpc('tools/call', {'name':'blueprint_query','arguments':{'project':self.map,'table':'entities','revision':page['revision']}})
        self.assertTrue(stale['isError'])
        self.client.call('task', project=self.map, action='resume')
        claimed = self.client.call('next', project=self.map, worker='reader')
        task = claimed['task']
        renewed = self.client.call('task', project=self.map, action='renew',task_id=task['id'],lease_id=task['lease']['id'])
        self.assertGreater(renewed['project']['revision'], claimed['project']['revision'])

    def test_bad_arguments_and_source_traversal_do_not_break_stdio(self):
        for name, args in [('read',{'project':self.map,'source':'../outside'}),
                           ('read',{'project':self.map,'source':'值.py','limit':True}),
                           ('status',{'project':'.codemap'}), ('init',{'root':str(self.root),'unexpected':1})]:
            response = self.client.rpc('tools/call', {'name':'blueprint_'+name,'arguments':args})
            self.assertTrue(response['isError'])
        self.assertEqual(self.client.rpc('ping',{}), {})
        self.assertEqual(self.client.call('status', project=self.map)['coverage']['sources_read'], 0)


class ProtocolAdapterTests(unittest.TestCase):
    def test_negotiation_lifecycle_and_unknown_methods(self):
        server = StdioServer()
        self.addCleanup(server.tools.close)
        self.assertEqual(server.handle([])['error']['code'], -32600)
        self.assertEqual(server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['error']['code'], -32000)
        result = server.handle({'jsonrpc':'2.0','id':2,'method':'initialize','params':
                               {'protocolVersion':'future','capabilities':{},'clientInfo':{'name':'x','version':'1'}}})
        self.assertEqual(result['result']['protocolVersion'], '2025-11-25')
        self.assertIsNone(server.handle({'jsonrpc':'2.0','method':'notifications/initialized'}))
        self.assertEqual(server.handle({'jsonrpc':'2.0','id':3,'method':'unknown'})['error']['code'], -32601)
        self.assertEqual(server.handle({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'unknown'}})['error']['code'], -32602)

    def test_build_rejects_non_plugin_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build(Path(tmp)/'user-data')
            target = Path(tmp)/'repository-blueprint'
            target.mkdir();(target/'keep.txt').write_text('user content')
            with self.assertRaises(ValueError):
                build(target, update=True)
            self.assertEqual((target/'keep.txt').read_text(), 'user content')


if __name__ == '__main__':
    unittest.main()
