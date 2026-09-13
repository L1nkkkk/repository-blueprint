from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest

from codemap.agent import AgentTools
from codemap.agent_views import status_view, claim_view
from codemap.core import ProtocolError
from codemap.project import open_project, status


class EfficientReadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'source'
        self.root.mkdir()
        (self.root / 'value.py').write_text('VALUE = 1\ndef value():\n    return VALUE\n', encoding='utf-8')
        self.tools = AgentTools()
        self.addCleanup(self.tools.close)
        self.project = self.tools.call('blueprint_init', {'root': str(self.root)})['project_directory']
        self.store = open_project(self.project)
        self.claim = self.call('next', worker='test-reader', include_pack=False)
        self.file_id = self.claim['entities'][0]['id']

    def call(self, name, **args):
        return self.tools.call('blueprint_' + name, {'project': self.project, **args})

    def changes(self):
        return {
            'sources': [{'key': 'source', 'source': 'value.py', 'read_state': 'read', 'symbols_complete': True}],
            'evidence': [{'key': 'body', 'source': 'value.py', 'start_line': 1, 'end_line': 3, 'note': 'Global constant and the function that returns it.'}],
            'entities': [{'id': self.file_id, 'key': 'file', 'analysis': 'reviewed', 'summary': 'Defines VALUE and value().', 'evidence_ids': ['$body']},
                         {'key': 'function', 'name': 'value', 'kind': 'function', 'source_ids': ['$source'], 'analysis': 'reviewed',
                          'summary': 'Returns the global VALUE.', 'evidence_ids': ['$body'],
                          'details': {'inputs': [], 'outputs': ['VALUE'], 'calls': [], 'reads': ['VALUE'], 'writes': [], 'conditions': []}}],
            'memberships': [{'key': 'member', 'parent_id': '$file', 'child_id': '$function', 'axis': 'semantic'}],
            'contexts': [{'key': 'context', 'parent_id': None, 'caller_id': None, 'callee_id': None, 'evidence_ids': ['$body']}],
            'ports': [{'key': 'output', 'entity_id': '$function', 'context_id': '$context', 'direction': 'out', 'channel': 'data', 'name': 'return', 'type': 'int', 'evidence_ids': ['$body']}],
            'flows': [{'key': 'return', 'name': 'returned VALUE', 'producer_port_id': '$output', 'value_version': 'return-1', 'derived_from': [], 'evidence_ids': ['$body']}],
            'relations': [{'key': 'reference', 'kind': 'reference', 'from_id': '$function', 'to_id': '$file', 'context_id': '$context', 'basis': 'source', 'freshness': 'current', 'evidence_ids': ['$body']}],
        }

    def prepare(self, **overrides):
        args = {'batch_template': self.claim['batch_template'], 'upserts': self.changes(), 'result': 'done', 'reason': 'Read the entire module and its function.'}
        args.update(overrides)
        return self.call('prepare', **args)

    def test_summary_bounds_lists_without_losing_real_counts_or_completion_state(self):
        full = status(self.store)
        full['last_update'] = {'plan_id': 'plan:x', 'base_revision': 0, 'records': ['x' * 1000] * 100}
        full['coverage']['incomplete_entities'] = ['entity:' + str(n) for n in range(1000)]
        before = deepcopy(full)
        small = status_view(full)
        self.assertEqual(small['project'], full['project'])
        self.assertEqual(small['coverage']['status'], full['coverage']['status'])
        self.assertEqual(small['task_counts'], full['task_counts'])
        self.assertEqual(small['coverage']['incomplete_entities']['count'], 1000)
        self.assertTrue(small['coverage']['incomplete_entities']['truncated'])
        self.assertLess(len(json.dumps(small)), len(json.dumps(full)) / 10)
        self.assertEqual(full, before)
        self.assertIs(status_view(full, 'full'), full)

    def test_claim_keeps_exact_template_and_exposes_truncated_scope(self):
        full = {'state': 'claimed', 'project': self.claim['project'], 'task': self.store.read()['tasks'][0],
                'sources': self.store.read()['sources'] * 25, 'entities': self.store.read()['entities'] * 25,
                'review_ids': {'entities': [str(n) for n in range(100)]}, 'update': {'huge': 'x' * 20000},
                'batch_template': self.claim['batch_template']}
        small = claim_view(full)
        self.assertEqual(small['batch_template'], full['batch_template'])
        self.assertEqual(small['source_count'], 25)
        self.assertEqual(len(small['sources']), 20)
        self.assertEqual(small['review_ids']['entities']['count'], 100)
        self.assertNotIn('update', small)
        self.assertIs(claim_view(full, 'full'), full)

    def test_missing_reads_and_pagination_gaps_cannot_prepare_completed_file(self):
        before = self.store.read()
        with self.assertRaises(ProtocolError):
            self.prepare()
        self.call('read', source='value.py', start=1, limit=1)
        self.call('read', source='value.py', start=3, limit=1)
        with self.assertRaisesRegex(ProtocolError, 'read these lines'):
            self.prepare()
        self.assertEqual(self.store.read(), before)
        self.call('read', source='value.py', start=2, limit=1)
        draft = self.prepare(detail='full')
        self.assertFalse(draft['committed'])
        self.assertEqual(self.store.read(), before)
        proof = draft['batch']['upserts']['evidence'][0]
        self.assertEqual(proof['sha256'], before['sources'][0]['sha256'])
        self.assertEqual(draft['batch']['upserts']['entities'][1]['language'], 'python')
        self.assertEqual(draft['batch']['upserts']['relations'][0]['from_id'], draft['aliases']['function'])

    def test_prepared_commit_survives_reconnect_and_retry_without_duplicate_progress(self):
        self.call('read', source='value.py')
        draft = self.prepare()
        self.assertEqual(self.prepare()['prepared_id'], draft['prepared_id'])
        other = AgentTools()
        self.addCleanup(other.close)
        args = {'project': self.project, 'prepared_id': draft['prepared_id']}
        inspected = other.call('blueprint_prepare', {**args, 'detail': 'full'})
        self.assertEqual(inspected['batch']['task_id'], self.claim['task']['id'])
        saved = other.call('blueprint_commit', args)
        again = other.call('blueprint_commit', args)
        self.assertEqual(saved['receipt'], again['receipt'])
        self.assertEqual(saved['project']['revision'], again['project']['revision'])
        self.assertEqual(saved['coverage']['sources_read'], 1)
        self.assertEqual(saved['task_counts']['queued'], 1)
        self.assertNotEqual(saved['coverage']['status'], 'complete')
        self.assertEqual(len(self.store.read()['receipts']), 1)

    def test_new_connection_cannot_author_evidence_from_old_connections_read(self):
        self.call('read', source='value.py')
        other = AgentTools()
        self.addCleanup(other.close)
        changes = {'evidence': self.changes()['evidence']}
        with self.assertRaisesRegex(ProtocolError, 'read these lines'):
            other.call('blueprint_prepare', {'project': self.project, 'batch_template': self.claim['batch_template'],
                                            'upserts': changes, 'result': 'partial', 'reason': 'Remaining work.'})

    def test_prepared_batches_recheck_disk_revision_and_lease(self):
        self.call('read', source='value.py')
        draft = self.prepare()
        original = (self.root / 'value.py').read_bytes()
        before = self.store.read()
        (self.root / 'value.py').write_text('changed = True\n', encoding='utf-8')
        with self.assertRaises(ProtocolError):
            self.call('commit', prepared_id=draft['prepared_id'])
        self.assertEqual(self.store.read(), before)
        (self.root / 'value.py').write_bytes(original)
        task = next(t for t in before['tasks'] if t['id'] == self.claim['task']['id'])
        renewed = self.call('task', action='renew', task_id=task['id'], lease_id=task['lease']['id'])
        with self.assertRaisesRegex(ProtocolError, 'revision'):
            self.call('commit', prepared_id=draft['prepared_id'])
        self.claim['batch_template']['base_revision'] = renewed['project']['revision']
        draft = self.prepare()
        # Move the validation clock past the lease; don't alter the stored graph.
        from unittest.mock import patch
        with patch('codemap.project.time.time', return_value=time.time() + 7200):
            with self.assertRaisesRegex(ProtocolError, 'expired'):
                self.call('commit', prepared_id=draft['prepared_id'])
        self.assertEqual(len(self.store.read()['receipts']), 0)

    def test_semantics_and_references_are_never_filled_to_fake_completion(self):
        self.call('read', source='value.py')
        changes = self.changes()
        del changes['entities'][1]['details']['conditions']
        with self.assertRaisesRegex(ProtocolError, 'function-level'):
            self.prepare(upserts=changes)
        changes = self.changes()
        changes['entities'][1]['evidence_ids'] = ['$nonexistent']
        with self.assertRaisesRegex(ProtocolError, 'unknown local reference'):
            self.prepare(upserts=changes)
        changes = self.changes()
        changes['flows'][0]['key'] = 'body'
        with self.assertRaisesRegex(ProtocolError, 'unique'):
            self.prepare(upserts=changes)
        self.assertEqual(len(self.store.read()['receipts']), 0)

    def test_context_reuse_checks_actual_disk_and_keeps_lookup_read_only(self):
        self.call('read', source='value.py')
        draft = self.prepare()
        self.call('commit', prepared_id=draft['prepared_id'])
        before = self.store.read()
        first = self.call('context', source='value.py', limit=1)
        self.assertEqual(first['records'][0]['reuse'], 'current_evidence')
        second = self.call('context', source='value.py', offset=first['next_offset'], revision=first['revision'])
        self.assertEqual(second['records'][0]['reuse'], 'current_evidence')
        (self.root / 'value.py').write_text('VALUE = 2\n', encoding='utf-8')
        changed = self.call('context', source='value.py')
        self.assertTrue(all(row['reuse'] == 'needs_review' for row in changed['records']))
        (self.root / 'value.py').unlink()
        missing = self.call('context', source='value.py')
        self.assertTrue(all(row['reuse'] == 'needs_review' for row in missing['records']))
        self.assertEqual(self.store.read(), before)

    def test_existing_fields_merge_but_original_drafts_stay_immutable(self):
        self.call('read', source='value.py')
        draft = self.prepare()
        original = self.call('prepare', prepared_id=draft['prepared_id'], detail='full')['batch']
        changed = self.changes()
        changed['entities'][0]['summary'] = 'Same module; revised wording.'
        revised = self.prepare(upserts=changed)
        self.assertNotEqual(revised['prepared_id'], draft['prepared_id'])
        self.assertEqual(self.call('prepare', prepared_id=draft['prepared_id'], detail='full')['batch'], original)
        self.assertEqual(original['upserts']['entities'][0]['source_ids'], [self.store.read()['sources'][0]['id']])
        with self.assertRaises(ProtocolError):
            self.call('commit', prepared_id=draft['prepared_id'], batch=original)

    def test_reused_context_and_port_evidence_still_checks_supporting_files(self):
        # A proof outside the claimed file must be checked even when reused only by a port/context.
        from codemap.project import initialize_project, claim_next, commit_result
        root = self.root.parent / 'supporting'
        root.mkdir()
        (root / 'a.py').write_text('a = 1\n')
        (root / 'b.py').write_text('b = 2\n')
        store = initialize_project(root)
        graph = store.read()
        source = next(s for s in graph['sources'] if s['path'] == 'b.py')
        proof = {'id': 'proof:b', 'source_id': source['id'], 'sha256': source['sha256'], 'start_line': 1, 'end_line': 1, 'note': 'Fixture supporting claim.'}
        claim = claim_next(store, 'support-reader')
        seed = deepcopy(claim['batch_template'])
        seed.update(upserts={'evidence': [proof]}, result='partial', reason='Fixture support saved; task unfinished.')
        commit_result(store, seed)
        claim = claim_next(store, 'support-reader', task_id=claim['task']['id'])
        template = claim['batch_template']
        context = {'id': 'context:x', 'parent_id': None, 'caller_id': None, 'callee_id': None}
        port = {'id': 'port:x', 'entity_id': claim['entities'][0]['id'], 'context_id': context['id'], 'direction': 'out', 'channel': 'data', 'name': 'x', 'type': 'int'}
        (root / 'b.py').write_text('b = 3\n')
        before = store.read()
        for table in ('contexts', 'ports'):
            with self.subTest(table=table):
                records = {'contexts': [deepcopy(context)], 'ports': [deepcopy(port)]}
                records[table][0]['evidence_ids'] = [proof['id']]
                batch = {**template, 'upserts': records, 'result': 'partial', 'reason': 'Reuse saved evidence.'}
                with self.assertRaisesRegex(ProtocolError, 'changed'):
                    commit_result(store, batch)
                self.assertEqual(store.read(), before)

    def test_canvas_reader_prepares_only_its_own_task_and_retries_its_commit(self):
        from codemap.reader_requests import enqueue, change_request, reader_mcp
        from unittest.mock import patch
        request = enqueue(self.store, {'id': 'efficiency', 'kind': 'repository', 'question': 'Continue saved work.'})
        change_request(self.store, request['id'], lambda r: r.update(state='running', run_token='active'))
        with patch('codemap.mcp.main', side_effect=lambda server: server):
            server = reader_mcp(self.project, request['id'], 'active')
        self.addCleanup(server.tools.close)
        with self.assertRaisesRegex(ProtocolError, '当前阅读者'):
            server.tools.call('blueprint_prepare', {'project': self.project, 'batch_template': self.claim['batch_template'],
                'upserts': {}, 'result': 'partial', 'reason': 'Must not claim someone else’s work.'})
        # Release the test-reader's lease, then acquire through the scoped host.
        graph = self.store.read()
        self.store.recover(expected_revision=graph['project']['revision'], now=time.time() + 7200)
        self.claim = server.tools.call('blueprint_next', {'project': self.project, 'worker': 'ignored'})
        server.tools.call('blueprint_read', {'project': self.project, 'source': 'value.py'})
        draft = server.tools.call('blueprint_prepare', {'project': self.project, 'batch_template': self.claim['batch_template'],
            'upserts': self.changes(), 'result': 'done', 'reason': 'Read module and function.'})
        args = {'project': self.project, 'prepared_id': draft['prepared_id']}
        saved = server.tools.call('blueprint_commit', args)
        self.assertEqual(server.tools.call('blueprint_commit', args)['receipt'], saved['receipt'])


if __name__ == '__main__':
    unittest.main()
