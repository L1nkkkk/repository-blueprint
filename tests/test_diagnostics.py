from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from codemap.agent import AgentTools
from codemap.core import ProtocolError, apply_batch, validate_graph
from codemap.diagnostics import DETAIL_FIELDS, ValidationErrors
from codemap.mcp import StdioServer
from codemap.metrics import failure_kind
from codemap.project import open_project


class PreparationDiagnosticsTests(unittest.TestCase):
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
        self.claim = self.call('next', worker='diagnostics-test', include_pack=False)
        self.file_id = self.claim['entities'][0]['id']
        self.before = self.store.read()

    def call(self, name, **args):
        return self.tools.call('blueprint_' + name, {'project': self.project, **args})

    def changes(self):
        return {'sources': [{'source': 'value.py', 'read_state': 'read', 'symbols_complete': True}],
                'evidence': [{'key': 'body', 'source': 'value.py', 'start_line': 1, 'end_line': 3, 'note': 'Constant and function body checked.'}],
                'entities': [{'id': self.file_id, 'analysis': 'reviewed', 'summary': 'Exports VALUE and value().', 'evidence_ids': ['$body']},
                             {'key': 'value', 'kind': 'function', 'name': 'value', 'source_ids': [self.before['sources'][0]['id']],
                              'analysis': 'reviewed', 'summary': 'Returns VALUE.', 'evidence_ids': ['$body'],
                              'details': {'inputs': [], 'outputs': ['VALUE'], 'calls': [], 'reads': ['VALUE'], 'writes': [], 'conditions': []}}]}

    def arguments(self, changes=None, **overrides):
        return {'project': self.project, 'batch_template': self.claim['batch_template'], 'upserts': self.changes() if changes is None else changes,
                'result': 'done', 'reason': 'Checked file and function.', **overrides}

    def prepare(self, changes=None, **overrides):
        return self.tools.call('blueprint_prepare', self.arguments(changes, **overrides))

    def assert_not_saved(self):
        self.assertEqual(self.store.read(), self.before)
        with closing(sqlite3.connect(self.store.path)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='prepared_batches'").fetchone()
            self.assertFalse(exists)

    def test_claim_supplies_unfilled_template_without_inventing_empty_findings(self):
        contract = self.claim['preparation_contract']
        self.assertEqual(contract['function_details_template'], dict.fromkeys(DETAIL_FIELDS))
        self.assertIn('file/module', contract['done_requirements']['analyze_update_scope'])
        self.assertEqual(self.store.read(), self.before)
        # A placeholder copied into a reviewed record must fail, not declare absence.
        self.call('read', source='value.py')
        changes = self.changes(); changes['entities'][1]['details'] = contract['function_details_template']
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        self.assertEqual({i['field'] for i in caught.exception.issues}, {'details.' + f for f in DETAIL_FIELDS})
        self.assert_not_saved()

    def test_missing_arrays_and_scope_source_states_report_together_and_repair_without_rereading(self):
        self.call('read', source='value.py')
        changes = self.changes()
        changes['sources'] = []
        changes['entities'][0].pop('analysis')
        del changes['entities'][1]['details']['conditions']
        changes['entities'][1]['details']['outputs'] = None
        original = deepcopy(changes)
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        rows = caught.exception.issues
        self.assertEqual({i['field'] for i in rows}, {'analysis', 'read_state', 'symbols_complete', 'details.conditions', 'details.outputs'})
        scope = next(i for i in rows if i['field'] == 'analysis')
        self.assertEqual(scope['record_id'], self.file_id)
        self.assertEqual(scope['record_name'], 'value.py')
        self.assertEqual(scope['source_paths'], ['value.py'])
        self.assertEqual(scope['expected'], 'reviewed')
        missing = next(i for i in rows if i['field'] == 'details.conditions')
        self.assertEqual(missing['current'], {'present': False})
        self.assertEqual(failure_kind(caught.exception), 'validation')
        self.assertFalse(any(i.get('requires_read') for i in rows))
        self.assertEqual(changes, original)
        self.assert_not_saved()
        # Only repair the findings. The same read ledger is reused for success.
        draft = self.prepare()
        saved = self.call('commit', prepared_id=draft['prepared_id'])
        self.assertEqual(saved['coverage']['sources_read'], 1)
        self.assertEqual(saved['task_counts']['done'], 1)

    def test_multiple_functions_name_exact_fields_and_preserve_details(self):
        self.call('read', source='value.py')
        changes = self.changes()
        second = deepcopy(changes['entities'][1]); second.update(key='other', name='other')
        changes['entities'].append(second)
        del changes['entities'][1]['details']['writes']
        del second['details']['calls']
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        self.assertEqual({(i['record_name'], i['field']) for i in caught.exception.issues}, {('value', 'details.writes'), ('other', 'details.calls')})
        self.assertEqual({(i['local_key'], i['input_path']) for i in caught.exception.issues},
                         {('value', 'upserts.entities[1]'), ('other', 'upserts.entities[2]')})
        self.assert_not_saved()

    def test_missing_source_ranges_and_field_problems_are_both_reported(self):
        self.call('read', source='value.py', start=1, limit=1)
        self.call('read', source='value.py', start=3, limit=1)
        changes = self.changes(); del changes['entities'][1]['details']['conditions']
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        reads = [i for i in caught.exception.issues if i['code'] == 'source_lines_not_read']
        self.assertTrue(reads)
        self.assertTrue(all(i['missing_ranges'] == [[2, 2]] for i in reads))
        self.assertIn('details.conditions', [i['field'] for i in caught.exception.issues])
        self.assertEqual(failure_kind(caught.exception), 'mixed')
        self.assert_not_saved()
        self.call('read', source='value.py', start=2, limit=1)
        self.assertIn('prepared_id', self.prepare())

    def test_unknown_local_references_report_paths_together_before_dependent_checks(self):
        changes = self.changes()
        changes['entities'][0]['evidence_ids'] = ['$absent_a']
        changes['entities'][1]['evidence_ids'] = ['$absent_b']
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        self.assertEqual(caught.exception.stage, 'local_references')
        self.assertEqual({i['field'] for i in caught.exception.issues}, {'entities[0].evidence_ids[0]', 'entities[1].evidence_ids[0]'})
        self.assertIn('findings and evidence', caught.exception.report()['pending_checks'])
        self.assert_not_saved()

    def test_duplicate_local_keys_report_identity_problem_without_partial_progress(self):
        changes = self.changes()
        changes['entities'][0]['key'] = 'body'
        changes['entities'][1]['key'] = 'body'
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        self.assertEqual(caught.exception.count, 2)
        self.assertTrue(all(i['code'] == 'record_identity' for i in caught.exception.issues))
        self.assert_not_saved()

    def test_long_failure_report_is_capped_and_reports_the_full_count(self):
        self.call('read', source='value.py')
        changes = self.changes()
        base = changes['entities'][1]; base['details'] = dict.fromkeys(DETAIL_FIELDS)
        changes['entities'] = [changes['entities'][0], *[{**base, 'key': f'func{i}', 'name': f'func{i}'} for i in range(20)]]
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(changes)
        report = caught.exception.report()
        self.assertEqual(report['issue_count'], 120)
        self.assertEqual(len(report['issues']), 40)
        self.assertTrue(report['truncated'])
        self.assert_not_saved()

    def test_raw_batch_and_graph_validation_keep_the_same_finding_guard(self):
        self.call('read', source='value.py')
        draft = self.prepare(detail='full')
        batch = deepcopy(draft['batch']); del batch['upserts']['entities'][1]['details']['reads']
        with self.assertRaises(ValidationErrors) as caught:
            apply_batch(self.before, batch, now=time.time())
        self.assertEqual(caught.exception.issues[0]['field'], 'details.reads')
        current = apply_batch(self.before, draft['batch'], now=time.time())
        del next(e for e in current['entities'] if e['kind'] == 'function')['details']['writes']
        with self.assertRaises(ValidationErrors):
            validate_graph(current)
        self.assertEqual(self.store.read(), self.before)

    def test_revision_and_expiry_failures_explain_identity_repair_without_weakening_guards(self):
        self.call('read', source='value.py')
        wrong = deepcopy(self.claim['batch_template']); wrong['base_revision'] -= 1
        with self.assertRaises(ValidationErrors) as caught:
            self.prepare(batch_template=wrong)
        self.assertEqual(caught.exception.category, 'revision')
        self.assertIn('reconcile', caught.exception.issues[0]['action'])
        with patch('codemap.project.time.time', return_value=time.time() + 7200):
            with self.assertRaises(ValidationErrors) as caught:
                self.prepare()
        self.assertEqual(caught.exception.category, 'lease')
        self.assert_not_saved()

    def test_stdio_errors_remain_errors_and_support_legacy_text_clients(self):
        self.call('read', source='value.py')
        changes = self.changes(); del changes['entities'][1]['details']['calls']
        for version in ('2025-11-25', '2024-11-05'):
            with self.subTest(version=version):
                server = StdioServer(self.tools)
                server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': version, 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
                server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                result = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                    'params': {'name': 'blueprint_prepare', 'arguments': self.arguments(changes)}})['result']
                self.assertTrue(result['isError'])
                report = json.loads(result['content'][0]['text'])
                self.assertEqual(report['issues'][0]['field'], 'details.calls')
                self.assertEqual(report['error'], 'validation_failed')
                if version == '2025-11-25':
                    self.assertEqual(result['structuredContent'], report)
                else:
                    self.assertNotIn('structuredContent', result)
                self.assertEqual(server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})['result'], {})
        self.assert_not_saved()


if __name__ == '__main__':
    unittest.main()
