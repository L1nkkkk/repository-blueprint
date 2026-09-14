from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from codemap.core import ProtocolError, validate_graph
from codemap.project import initialize_project, preview_update, apply_update
from codemap.repository import scan_repository, entity
from codemap.store import GraphStore
from codemap.change_analysis import snapshot_texts
from codemap.coverage import coverage_report
from codemap.history import restore_preview, restore_snapshot
from codemap.reader_requests import enqueue, request_by_id, control_request, initialize_requests, worker_main, change_request
from examples.review_sample import BASE, replay
from examples.review_update import MOTION


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        shutil.copytree(BASE / 'sample_repo', self.root)
        # History diff/restore explicitly exercises the compatibility snapshot
        # cache; the scaled default path is covered by repository regressions.
        self.store = initialize_project(self.root, self.base / 'map', legacy_cache=True); replay(self.store)
        self.before = self.store.read()

    def apply(self, renames=()):
        plan = preview_update(self.store, renames=renames)
        return apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'], renames=renames)

    def test_exact_rename_preserves_source_file_function_flow_and_view_identities(self):
        self.store.save_view('canvas', {'layouts': {'custom': {'positions': {'fn:move': {'x': 9, 'y': 2}}}}})
        old = next(s for s in self.before['sources'] if s['path'] == 'src/motion.cpp')
        old_file = next(e for e in self.before['entities'] if e['kind'] == 'file' and old['id'] in e['source_ids'])
        (self.root / 'src/motion.cpp').rename(self.root / 'src/renamed.cpp')
        plan = preview_update(self.store)
        self.assertEqual(plan['renamed'], [{'from': 'src/motion.cpp', 'to': 'src/renamed.cpp', 'basis': 'identical_content'}])
        self.assertEqual(plan['added'], []); self.assertEqual(plan['deleted'], [])
        self.apply(); graph = self.store.read(); validate_graph(graph, self.root)
        moved = next(s for s in graph['sources'] if s['path'] == 'src/renamed.cpp')
        self.assertEqual(moved['id'], old['id'])
        self.assertEqual(next(e for e in graph['entities'] if e['id'] == old_file['id'])['name'], 'renamed.cpp')
        for table in ('flows', 'ports', 'contexts', 'relations'):
            self.assertEqual({r['id'] for r in graph[table]}, {r['id'] for r in self.before[table]})
        self.assertEqual(self.store.read_view('canvas')['layouts']['custom']['positions']['fn:move']['x'], 9)

    def test_edited_move_requires_explicit_mapping_and_rejects_conflicting_pairs(self):
        (self.root / 'src/motion.cpp').unlink()
        (self.root / 'src/renamed.cpp').write_text(MOTION, encoding='utf-8')
        self.assertEqual(preview_update(self.store)['renamed'], [])
        mapping = [{'from': 'src/motion.cpp', 'to': 'src/renamed.cpp'}]
        plan = preview_update(self.store, renames=mapping)
        with self.assertRaises(ProtocolError):
            apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        with self.assertRaises(ProtocolError): preview_update(self.store, renames=mapping * 2)
        self.apply(mapping)
        graph = self.store.read(); validate_graph(graph, self.root)
        self.assertTrue(any(e['id'] == 'fn:move' and e['freshness'] == 'stale' for e in graph['entities']))

    def test_identical_copies_are_not_guessed_to_be_renames(self):
        original = (self.root / 'tools/report.cpp').read_bytes()
        (self.root / 'tools/report.cpp').unlink()
        (self.root / 'tools/one.cpp').write_bytes(original)
        (self.root / 'tools/two.cpp').write_bytes(original)
        plan = preview_update(self.store)
        self.assertFalse(plan['renamed']); self.assertEqual(plan['deleted'], ['tools/report.cpp'])

    def test_history_restore_archives_current_preserves_code_and_views_and_marks_stale_evidence(self):
        self.store.save_view('canvas', {'theme': 'dark', 'panels': {'details': {'width': 670}}, 'layouts': {'a': {'positions': {}}}})
        (self.root / 'src/motion.cpp').write_text(MOTION, encoding='utf-8'); self.apply()
        current, view = self.store.read(), self.store.read_view('canvas')
        p = restore_preview(self.store, self.before['project']['revision'])
        self.assertTrue(p['changes']['sources']['modified'])
        self.assertTrue(any(d['available'] and 'next + 1.0f' in d['diff'] for d in p['source_diffs']))
        result = restore_snapshot(self.store, p['revision'], expected_revision=p['current_revision'], restore_id=p['restore_id'])
        self.assertEqual(result['revision'], current['project']['revision'] + 1)
        repeated = restore_snapshot(self.store, p['revision'], expected_revision=p['current_revision'], restore_id=p['restore_id'])
        self.assertTrue(repeated['reused']); self.assertEqual(repeated['revision'], result['revision'])
        self.assertEqual(self.store.read_snapshot(current['project']['revision']), current)
        self.assertEqual(self.store.read_view('canvas'), view)
        self.assertEqual((self.root / 'src/motion.cpp').read_text(encoding='utf-8'), MOTION)
        self.assertTrue(any(e.get('freshness') == 'stale' for e in self.store.read()['evidence']))
        validate_graph(self.store.read(), self.root)

    def test_stale_restore_preview_and_running_reader_cannot_overwrite_graph(self):
        (self.root / 'src/motion.cpp').write_text(MOTION, encoding='utf-8'); self.apply()
        p = restore_preview(self.store, self.before['project']['revision'])
        (self.root / 'src/diagnostics.cpp').write_text('int changed = 4;')
        with self.assertRaises(ProtocolError): restore_snapshot(self.store, p['revision'], expected_revision=p['current_revision'], restore_id=p['restore_id'])
        p = restore_preview(self.store, p['revision'])
        enqueue(self.store, {'id': 'running-test', 'kind': 'question', 'question': '读取这个仓库'})
        change_request(self.store, 'running-test', lambda r: r.update(state='running'))
        graph = self.store.read()
        with self.assertRaisesRegex(ProtocolError, '暂停'): restore_snapshot(self.store, p['revision'], expected_revision=p['current_revision'], restore_id=p['restore_id'])
        self.assertEqual(self.store.read(), graph)

    def test_restore_keeps_previously_associated_edited_move_identity(self):
        old = next(s for s in self.before['sources'] if s['path'] == 'src/motion.cpp')
        (self.root / 'src/motion.cpp').unlink()
        (self.root / 'src/renamed.cpp').write_text(MOTION, encoding='utf-8')
        self.apply([{'from': 'src/motion.cpp', 'to': 'src/renamed.cpp'}])
        p = restore_preview(self.store, self.before['project']['revision'])
        self.assertEqual(p['source_update']['rename_approvals'], [{'from': 'src/motion.cpp', 'to': 'src/renamed.cpp'}])
        restore_snapshot(self.store, p['revision'], expected_revision=p['current_revision'], restore_id=p['restore_id'])
        graph = self.store.read(); validate_graph(graph, self.root)
        self.assertEqual(next(s for s in graph['sources'] if s['path'] == 'src/renamed.cpp')['id'], old['id'])
        self.assertTrue(any(e['id'] == 'fn:move' for e in graph['entities']))

    def test_scoped_reader_rejects_another_map_and_old_execution_identity(self):
        from codemap.reader_requests import reader_mcp
        enqueue(self.store, {'id': 'scoped', 'kind': 'question', 'question': '读取文件'})
        change_request(self.store, 'scoped', lambda r: r.update(state='running', run_token='first'))
        directory = Path(self.store.path).parent
        with patch('codemap.mcp.main', side_effect=lambda server: server):
            server = reader_mcp(directory, 'scoped', 'first')
        self.assertTrue(all(t['name'] not in {'blueprint_next', 'blueprint_commit', 'blueprint_init', 'blueprint_export'} for t in server.catalog))
        with self.assertRaisesRegex(ProtocolError, '当前请求'):
            server.tools.call('blueprint_status', {'project': str(directory / 'another')})
        change_request(self.store, 'scoped', lambda r: r.update(run_token='second'))
        with self.assertRaisesRegex(ProtocolError, '执行身份'):
            server.tools.call('blueprint_status', {'project': str(directory)})

    def test_restarted_worker_releases_only_its_interrupted_request_lease(self):
        from codemap.reader_requests import ensure_analysis_task
        from codemap.project import claim_next
        target = next(e['id'] for e in self.before['entities'] if e['kind'] == 'function')
        request = enqueue(self.store, {'id': 'interrupted', 'kind': 'analyze', 'question': '继续核对', 'entity_id': target})
        task_id = ensure_analysis_task(self.store, request)
        claim_next(self.store, 'canvas:interrupted', task_id=task_id)
        change_request(self.store, 'interrupted', lambda r: r.update(state='running', run_token='old', stop_requested='pause'))
        with patch('codemap.reader_requests.execute_turn') as execute, patch('codemap.reader_requests.time.sleep'):
            worker_main(Path(self.store.path).parent)
        execute.assert_not_called()
        result = request_by_id(self.store, 'interrupted')
        self.assertEqual(result['state'], 'paused'); self.assertIsNone(result['run_token'])
        selected = next(t for t in self.store.read()['tasks'] if t['id'] == task_id)
        self.assertEqual(selected['state'], 'queued'); self.assertIsNone(selected['lease'])

    def test_coverage_separates_reading_scenarios_and_uncertain_relations(self):
        report = coverage_report(self.before)
        self.assertEqual(report['reading']['sources_read'], report['reading']['sources_total'])
        self.assertGreater(report['scenario_count'], 0)
        self.assertLess(report['callables_in_scenarios'], report['callable_count'])
        self.assertTrue(report['outside_scenarios'])
        changed = deepcopy(self.before)
        changed['relations'][0].update(basis='unresolved', reason='dynamic callback')
        fresh = coverage_report(changed)
        self.assertTrue(any(r['reason'] == 'dynamic callback' for r in fresh['pending_relations']))
        self.assertEqual(fresh['reading']['sources_read'], report['reading']['sources_read'])
        self.assertEqual(self.store.read(), self.before)

    def test_requests_are_idempotent_durable_and_pause_without_changing_reading_progress(self):
        data = {'id': 'stable-request', 'kind': 'question', 'question': '输入在哪里使用？'}
        first = enqueue(self.store, data); self.assertEqual(enqueue(self.store, data), first)
        with self.assertRaises(ProtocolError): enqueue(self.store, {**data, 'question': '另一问题'})
        self.assertEqual(control_request(self.store, data['id'], 'pause')['state'], 'paused')
        reopened = GraphStore(self.store.path)
        self.assertEqual(request_by_id(reopened, data['id'])['state'], 'paused')
        self.assertEqual(control_request(reopened, data['id'], 'resume')['state'], 'queued')
        self.assertEqual(control_request(reopened, data['id'], 'cancel')['state'], 'cancelled')
        self.assertEqual(self.store.read(), self.before)

    def test_a_reader_answer_does_not_claim_unsubmitted_analysis_is_complete(self):
        target = next(e['id'] for e in self.before['entities'] if e['kind'] == 'function')
        enqueue(self.store, {'id': 'no-commit', 'kind': 'analyze', 'question': '拆解函数', 'entity_id': target})
        with patch('codemap.reader_requests.execute_turn', return_value={'result': '没有提交批次'}), patch('codemap.reader_requests.time.sleep'):
            worker_main(Path(self.store.path).parent)
        result = request_by_id(self.store, 'no-commit')
        self.assertEqual(result['state'], 'needs_attention')
        self.assertTrue(any(t['id'] == 'task:canvas:no-commit' and t['state'] == 'queued' for t in self.store.read()['tasks']))


class PreciseChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = self.root = Path(self.temp.name) / 'repo'; root.mkdir()
        (root / 'a.py').write_text('def selected():\n    return 1  # old note\n\ndef unrelated():\n    return 2\n')
        (root / 'b.py').write_text('def caller():\n    return selected()\n')
        (root / 'c.py').write_text('def other():\n    return unrelated()\n')
        graph = scan_repository(root); sources = {s['path']: s for s in graph['sources']}
        file_ids = {e['source_ids'][0]: e['id'] for e in graph['entities'] if e['kind'] == 'file'}
        for id, path, start, end in [('selected', 'a.py', 1, 2), ('unrelated', 'a.py', 4, 5), ('caller', 'b.py', 1, 2), ('other', 'c.py', 1, 2)]:
            source = sources[path]; ev = 'ev:' + id
            row = entity('function', id, sources=[source['id']], language='python'); row.update(id=id, evidence_ids=[ev])
            graph['entities'].append(row)
            graph['evidence'].append({'id': ev, 'source_id': source['id'], 'sha256': source['sha256'], 'start_line': start, 'end_line': end, 'note': 'test fixture'})
            graph['memberships'].append({'id': 'member:' + id, 'axis': 'semantic', 'parent_id': file_ids[source['id']], 'child_id': id})
        graph['contexts'].append({'id': 'root', 'parent_id': None, 'caller_id': None, 'callee_id': None})
        for caller, callee in [('caller', 'selected'), ('other', 'unrelated')]:
            ctx = 'ctx:' + caller
            graph['contexts'].append({'id': ctx, 'parent_id': 'root', 'caller_id': caller, 'callee_id': callee, 'callsite_evidence_id': 'ev:' + caller})
            graph['relations'].append({'id': 'call:' + caller, 'kind': 'call', 'from_id': caller, 'to_id': callee, 'context_id': ctx, 'basis': 'source', 'freshness': 'current', 'evidence_ids': ['ev:' + caller]})
        validate_graph(graph)
        self.store = GraphStore(Path(self.temp.name) / 'map.sqlite'); self.store.initialize(graph)
        self.store.save_source_texts(snapshot_texts(graph))

    def test_comment_edit_stays_local_and_implementation_edit_follows_only_its_callers(self):
        path = self.root / 'a.py'; old = path.read_text()
        path.write_text(old.replace('old note', 'clearer note'))
        plan = preview_update(self.store)
        self.assertEqual(plan['change_details'][0]['kind'], 'cosmetic')
        self.assertEqual(plan['affected_paths'], ['a.py'])
        path.write_text(old.replace('return 1', 'return 3'))
        plan = preview_update(self.store)
        self.assertEqual(plan['change_details'][0]['kind'], 'implementation')
        self.assertEqual(plan['affected_paths'], ['a.py', 'b.py'])
        path.write_text(old.replace('selected():', 'selected(value):'))
        plan = preview_update(self.store)
        self.assertEqual(plan['change_details'][0]['kind'], 'contract')
        self.assertEqual(plan['affected_paths'], ['a.py', 'b.py', 'c.py'])
