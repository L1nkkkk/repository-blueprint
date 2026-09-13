from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codemap.agent import AgentTools
from codemap.metrics import Recorder, FILENAME, report, add_range
from codemap.mcp import StdioServer
from codemap.project import initialize_project


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'; self.root.mkdir()
        (self.root / 'sample.py').write_text('secret_source_value = 1\nx = 2\ny = 3\n', encoding='utf-8')
        self.store = initialize_project(self.root, self.base / 'map')
        self.project = str(self.base / 'map')
        self.tools = AgentTools(); self.addCleanup(self.tools.close)

    def call(self, name, **args):
        return self.tools.call('blueprint_' + name, {'project': self.project, **args})

    def test_old_maps_are_empty_not_zero_duration_and_report_has_no_writes(self):
        before = self.store.read()
        self.assertIsNone(report(self.store)['summary'])
        self.assertEqual(self.call('metrics')['state'], 'empty')
        self.assertFalse((self.base / 'map' / FILENAME).exists())
        self.assertEqual(self.store.read(), before)

    def test_actual_reading_pack_overlap_and_partial_overlap_do_not_mark_review_complete(self):
        self.store.save_view('canvas', {'theme': 'dark', 'custom': 7})
        self.call('pack', source='sample.py', related_limit=0)
        self.call('read', source='sample.py', start=2, limit=2)
        data = self.call('metrics')
        self.assertEqual(data['summary']['read_lines'], 5)
        self.assertEqual(data['summary']['repeated_lines'], 2)
        self.assertEqual(data['progress']['sources_read'], 0)
        self.assertEqual(self.store.read()['project']['revision'], 0)
        self.assertEqual(self.store.read_view('canvas'), {'theme': 'dark', 'custom': 7})
        self.assertEqual(self.call('metrics')['summary']['calls'], 2)

    def test_overlap_counts_unique_intersections_and_hashes_separately(self):
        ranges = {}
        self.assertEqual(add_range(ranges, ('a', 'hash1'), 1, 5), (5, 0))
        self.assertEqual(add_range(ranges, ('a', 'hash1'), 4, 9), (6, 2))
        self.assertEqual(add_range(ranges, ('a', 'hash1'), 3, 7), (5, 5))
        self.assertEqual(add_range(ranges, ('a', 'hash2'), 1, 5), (5, 0))

    def test_monotonic_tool_and_gap_clocks_exclude_recording_and_wall_clock_changes(self):
        clock, wall = [10.0], [1000.0]
        recorder = Recorder(clock=lambda: clock[0], wall=lambda: wall[0])
        first = recorder.begin(self.store, 'status', {'project': self.project})
        clock[0] += .25; first.finish({})
        clock[0] += 3; wall[0] -= 100
        second = recorder.begin(self.store, 'query', {'project': self.project})
        clock[0] += .5; second.finish({})
        data = report(self.store, session=recorder.id)
        self.assertAlmostEqual(data['summary']['tool_ms'], 750)
        self.assertAlmostEqual(data['summary']['between_calls_ms'], 3000)
        self.assertNotIn('reasoning_ms', data['summary'])
        recorder.close()

    def test_failed_identical_requests_are_counted_without_storing_arguments_or_error_text(self):
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.call('query', table='entities', revision=9876)
        self.call('search', query='sensitive-search-phrase')
        data = report(self.store)
        self.assertEqual(data['summary']['errors'], 2)
        self.assertEqual(data['summary']['retries'], 1)
        self.assertEqual(data['summary']['repeated_requests'], 1)
        raw = (self.base / 'map' / FILENAME).read_bytes()
        self.assertNotIn(b'sensitive-search-phrase', raw)
        self.assertNotIn(b'secret_source_value', raw)
        self.assertNotIn('arguments', data['events'][0])

    def test_prepared_commit_is_attributed_and_duplicate_receipts_count_once(self):
        claim = self.call('next', worker='wb-test', detail='full')
        self.assertEqual(report(self.store)['progress']['claimed_tasks'][0]['paths'], ['sample.py'])
        batch = deepcopy(claim['batch_template'])
        batch.update(result='partial', reason='Read source; semantic analysis remains.', upserts={})
        draft = self.store.save_prepared(batch)
        self.call('commit', prepared_id=draft)
        self.call('commit', prepared_id=draft)
        data = report(self.store)
        self.assertEqual(data['summary']['observed_batches'], 1)
        self.assertEqual(data['summary']['successful_commit_calls'], 2)
        self.assertEqual(data['tasks'][0]['task_id'], claim['task']['id'])
        self.assertEqual(data['tasks'][0]['current_state'], 'queued')
        self.assertEqual(data['progress']['sources_read'], 0)

    def test_sessions_survive_reconnection_and_reports_do_not_mix_them(self):
        self.tools.set_client_info({'name': 'WorkBuddy-test', 'version': '1'})
        self.call('read', source='sample.py')
        old = self.tools.metrics.id
        self.tools.close()
        fresh = AgentTools(); self.addCleanup(fresh.close)
        fresh.set_client_info({'name': 'another-host'})
        fresh.call('blueprint_read', {'project': self.project, 'source': 'sample.py'})
        data = report(self.store)
        self.assertEqual(data['selected']['client'], 'another-host')
        self.assertEqual(data['summary']['repeated_lines'], 0)
        previous = report(self.store, session=old)
        self.assertEqual(previous['selected']['client'], 'WorkBuddy-test')
        self.assertIsNotNone(previous['selected']['closed'])
        self.assertEqual(previous['summary']['calls'], 1)

    def test_one_connection_keeps_different_maps_and_read_counters_separate(self):
        second = initialize_project(self.root, self.base / 'second-map')
        self.call('read', source='sample.py')
        self.tools.call('blueprint_read', {'project': str(self.base / 'second-map'), 'source': 'sample.py'})
        self.call('read', source='sample.py')
        self.assertEqual(report(self.store)['summary']['repeated_lines'], 3)
        self.assertEqual(report(second)['summary']['repeated_lines'], 0)
        self.assertEqual(report(second)['summary']['calls'], 1)

    def test_source_manifest_change_is_visible_when_comparing_an_old_connection(self):
        from codemap.project import preview_update, apply_update
        self.call('read', source='sample.py')
        self.assertTrue(report(self.store)['snapshot_matches_start'])
        (self.root / 'sample.py').write_text('x = 999\n', encoding='utf-8')
        plan = preview_update(self.store)
        apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertFalse(report(self.store)['snapshot_matches_start'])

    def test_unfinished_record_is_visible_without_claiming_a_model_is_running(self):
        span = self.tools.metrics.begin(self.store, 'read', {'project': self.project, 'source': 'sample.py'})
        data = report(self.store)
        self.assertEqual(data['summary']['unfinished'], 1)
        self.assertIsNone(data['events'][0]['elapsed_ms'])
        self.assertIsNone(data['selected']['closed'])
        span.finish(error=OSError('not stored'))
        self.assertEqual(report(self.store)['summary']['unfinished'], 0)

    def test_failure_to_write_metrics_does_not_prevent_a_real_commit(self):
        claim = self.call('next', worker='test', include_pack=False)
        batch = claim['batch_template']; batch.update(result='partial', reason='Continue reading')
        with patch('codemap.metrics.connect', side_effect=sqlite3.OperationalError('locked')):
            saved = self.call('commit', batch=batch)
        self.assertIn(batch['batch_id'], self.store.read()['receipts'])
        self.assertEqual(saved['receipt']['batch_id'], batch['batch_id'])
        self.assertEqual(self.call('read', source='sample.py')['total_lines'], 3)
        self.assertGreater(report(self.store)['selected']['dropped'], 0)

    def test_report_pagination_keeps_full_summary_and_rejects_unknown_session(self):
        for _ in range(3): self.call('read', source='sample.py')
        data = report(self.store, limit=1)
        self.assertEqual(len(data['events']), 1)
        self.assertEqual(data['summary']['calls'], 3)
        self.assertTrue(data['events_truncated'])
        with self.assertRaises(ValueError): report(self.store, session='missing')

    def test_stdio_client_identity_and_protocol_response_stay_compatible(self):
        server = StdioServer(self.tools)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'wb-client', 'version': '1'}}})
        server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        output = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'blueprint_read', 'arguments': {'project': self.project, 'source': 'sample.py'}}})['result']
        self.assertFalse(output['isError'])
        self.assertEqual(json.loads(output['content'][0]['text']), output['structuredContent'])
        self.assertEqual(report(self.store)['selected']['client'], 'wb-client')


if __name__ == '__main__':
    unittest.main()
