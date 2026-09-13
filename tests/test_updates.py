from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from codemap.core import ProtocolError, validate_graph, completion_report
from codemap.project import initialize_project, preview_update, apply_update, claim_next, commit_result, canvas_graph
from codemap.updates import scan_update, update_plan
from examples.review_sample import BASE, replay
from examples.review_update import MOTION, review_plus_one


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '中文 仓库'
        shutil.copytree(BASE / 'sample_repo', self.root)
        self.store = initialize_project(self.root, Path(self.temp.name) / 'map')
        replay(self.store)
        self.before = self.store.read()
        self.view = {'theme': 'dark', 'focus': ['flow:next-1'], 'scope': 'ctx:move-1',
                     'layouts': {'workflow:ctx:move-1': {'positions': {'call:ctx:integrate-1': {'x': 421, 'y': 71, 'locked': True}},
                                                        'viewport': {'x': 30, 'y': 50, 'z': .8}}}}
        self.store.save_view('canvas', self.view)

    def modify(self):
        (self.root / 'src/motion.cpp').write_bytes(MOTION.encode('utf-8'))

    def apply(self):
        plan = preview_update(self.store)
        return apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])

    def test_motion_change_targets_its_caller_but_preserves_independent_modules_and_views(self):
        self.modify(); plan = preview_update(self.store)
        self.assertEqual(plan['changed'], ['src/motion.cpp'])
        self.assertEqual(plan['affected_paths'], ['src/motion.cpp', 'tests/test_motion.cpp'])
        self.assertEqual(self.store.read(), self.before)
        projected = canvas_graph(self.store)
        self.assertEqual(next(e for e in projected['graph']['entities'] if e['id'] == 'fn:main')['freshness'], 'stale')
        self.assertEqual(next(e for e in projected['graph']['entities'] if e['id'] == 'fn:measure')['freshness'], 'current')
        self.apply(); after = self.store.read()
        self.assertEqual(self.store.read_snapshot(self.before['project']['revision']), self.before)
        self.assertEqual(self.store.read_view('canvas'), self.view)
        for id in ('fn:measure', 'fn:display'):
            self.assertEqual(next(e for e in after['entities'] if e['id'] == id), next(e for e in self.before['entities'] if e['id'] == id))
        self.assertEqual({f['id']: f['color_key'] for f in after['flows']}, {f['id']: f['color_key'] for f in self.before['flows']})
        self.assertEqual(completion_report(after)['sources_read'], 2)
        self.assertEqual(next(e for e in after['evidence'] if e['id'] == 'ev:move')['sha256'], next(e for e in self.before['evidence'] if e['id'] == 'ev:move')['sha256'])
        self.assertEqual(next(e for e in after['evidence'] if e['id'] == 'ev:move')['freshness'], 'stale')
        validate_graph(after, self.root)

    def test_canvas_marks_root_scenario_evidence_stale_without_rewriting_saved_map(self):
        graph = deepcopy(self.before)
        context = next(c for c in graph['contexts'] if not c.get('parent_id'))
        context.update(evidence_ids=['ev:move'], freshness='current')
        validate_graph(graph)
        self.modify()
        with patch.object(self.store, 'read', return_value=graph):
            result = canvas_graph(self.store)
        projected = next(c for c in result['graph']['contexts'] if c['id'] == context['id'])
        self.assertEqual(projected['freshness'], 'stale')
        self.assertEqual(context['freshness'], 'current')
        self.assertEqual(self.store.read(), self.before)
        self.assertEqual(result['status']['coverage']['status'], 'snapshot_changed')
        validate_graph(result['graph'])

    def test_rereading_changes_derived_flow_and_assertion_claim_without_rebuilding_other_files(self):
        self.modify(); self.apply()
        result = review_plus_one(self.store)
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertEqual(result['read_paths'], ['src/motion.cpp', 'tests/test_motion.cpp'])
        final = self.store.read(); validate_graph(final, self.root)
        flows = {f['id']: f for f in final['flows']}
        self.assertEqual(flows['flow:next-1']['derived_from'], ['flow:integrated-1'])
        self.assertEqual(flows['flow:next-1']['color_key'], 'D4')
        self.assertIn('flow:next-1', flows['flow:integrated-2']['derived_from'])
        self.assertIn('6', next(e for e in final['entities'] if e['id'] == 'fn:main')['summary'])
        self.assertEqual(self.store.read_view('canvas'), self.view)

    def test_preview_is_invalidated_by_new_source_changes_or_another_writer(self):
        self.modify(); plan = preview_update(self.store)
        (self.root / 'tools/report.cpp').write_text('int changed = 1;')
        with self.assertRaisesRegex(ProtocolError, 'preview changes again'):
            apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertEqual(self.store.read(), self.before); self.assertEqual(self.store.snapshot_history(), [])
        plan = preview_update(self.store)
        self.store.set_paused(True, expected_revision=plan['base_revision'])
        with self.assertRaisesRegex(ProtocolError, 'stale project revision'):
            apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertEqual(self.store.snapshot_history(), [])

    def test_no_change_is_noop_and_successful_update_retry_is_idempotent(self):
        self.apply(); self.assertEqual(self.store.read(), self.before); self.assertEqual(self.store.snapshot_history(), [])
        self.modify(); plan = preview_update(self.store)
        first = apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        second = apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertEqual(first['project'], second['project']); self.assertTrue(second['reused'])
        self.assertEqual(len(self.store.snapshot_history()), 1)

    def test_own_new_map_directory_does_not_trigger_a_false_code_update(self):
        root = Path(self.temp.name) / 'simple'; root.mkdir(); (root/'a.py').write_text('x=1')
        store = initialize_project(root)
        self.assertEqual(preview_update(store)['state'], 'current')

    def test_scan_gap_cannot_remove_unreadable_paths_or_archive_a_half_inventory(self):
        self.modify(); fresh = scan_update(self.before)
        fresh['inventory']['gaps'].append({'path':'src', 'reason':'unreadable'})
        fresh['project']['inventory_complete'] = False
        plan = update_plan(self.before, fresh)
        self.assertFalse(plan['can_apply'])
        with patch('codemap.project.scan_update', return_value=fresh):
            with self.assertRaisesRegex(ProtocolError, 'Inventory has gaps'):
                apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        self.assertEqual(self.store.read(), self.before); self.assertEqual(self.store.snapshot_history(), [])

    def test_new_and_deleted_sources_update_inventory_and_remove_dangling_facts(self):
        (self.root/'src/motion.cpp').unlink()
        (self.root/'extra').mkdir(); (self.root/'extra/new.py').write_text('value = 3\n')
        plan = preview_update(self.store)
        self.assertEqual(plan['deleted'], ['src/motion.cpp']); self.assertEqual(plan['added'], ['extra/new.py'])
        self.apply(); graph = self.store.read(); validate_graph(graph, self.root)
        self.assertNotIn('fn:move', {e['id'] for e in graph['entities']})
        self.assertFalse(any(c.get('callee_id') == 'fn:move' for c in graph['contexts']))
        self.assertIn('extra/new.py', {s['path'] for s in graph['sources']})
        self.assertTrue(any(t['kind'] == 'update' and t['state'] == 'queued' for t in graph['tasks']))
        self.assertNotEqual(completion_report(graph)['status'], 'complete')
        self.assertEqual(self.store.read_view('canvas'), self.view)

    def test_old_leases_and_unreviewed_or_stale_evidence_cannot_complete_new_tasks(self):
        self.modify(); self.apply(); claimed = claim_next(self.store, 'reader')
        old_batch = deepcopy(claimed['batch_template'])
        # A second edit invalidates every lease tied to the previous snapshot.
        (self.root/'src/motion.cpp').write_text(MOTION+'\n')
        self.apply()
        with self.assertRaisesRegex(ProtocolError, 'snapshot mismatch'):
            commit_result(self.store, old_batch)
        claimed = claim_next(self.store, 'reader2'); before = self.store.read()
        batch = claimed['batch_template']; batch.update(result='done', reason='No actual reread')
        with self.assertRaises(ProtocolError): commit_result(self.store, batch)
        self.assertEqual(self.store.read(), before)
        entity = deepcopy(next(e for e in before['entities'] if e['id'] == 'fn:move'))
        entity['freshness'] = 'current'; batch.update(result='partial', upserts={'entities':[entity]})
        with self.assertRaisesRegex(ProtocolError, 'stale evidence'): commit_result(self.store, batch)
        self.assertEqual(self.store.read(), before)

    def test_symbol_retirement_is_scoped_and_removes_dependent_ports_calls_and_flows(self):
        self.modify(); self.apply(); claimed = claim_next(self.store, 'reader')
        batch = claimed['batch_template']; batch.update(deletes={'entities':['fn:display']}, reason='Remove unrelated symbol')
        before = self.store.read()
        with self.assertRaisesRegex(ProtocolError, 'outside this update task'): commit_result(self.store, batch)
        self.assertEqual(self.store.read(), before)
        batch.update(deletes={'entities':['fn:move']}, reason='Retire the affected symbol and dependent old graph records')
        commit_result(self.store, batch)
        graph = self.store.read(); validate_graph(graph)
        self.assertNotIn('fn:move', {e['id'] for e in graph['entities']})
        self.assertFalse(any(p['entity_id'] == 'fn:move' for p in graph['ports']))
        self.assertNotEqual(completion_report(graph)['status'], 'complete')

    def test_build_change_rechecks_included_files_even_without_recorded_calls(self):
        target = self.root/'CMakeLists.txt'; target.write_text(target.read_text()+'\n# changed configuration\n')
        self.assertEqual(set(preview_update(self.store)['affected_paths']), {s['path'] for s in self.before['sources']})

    def test_deleting_all_files_leaves_an_explicit_empty_repository_review(self):
        for source in self.before['sources']:
            (self.root/source['path']).unlink()
        self.apply()
        claimed = claim_next(self.store, 'empty-repository-reader')
        self.assertEqual(claimed['task']['id'], 'task:repository-review')
        graph = self.store.read()
        rows = [dict(e, analysis=e['analysis'] if e['kind'] == 'external' else 'reviewed', freshness='current', summary='完整目录扫描确认所有源文件已删除，当前仓库无文件。', evidence_ids=[]) for e in graph['entities']]
        batch = claimed['batch_template']; batch.update(upserts={'entities':rows}, deletes={'ports':claimed['review_ids'].get('ports', [])}, result='done', reason='Empty inventory verified; obsolete boundary ports retired')
        commit_result(self.store, batch)
        self.assertEqual(completion_report(self.store.read())['status'], 'complete')
        self.assertEqual(self.store.read_snapshot(self.before['project']['revision']), self.before)

    def test_apply_requires_a_reviewable_plan_identity(self):
        with self.assertRaisesRegex(ProtocolError, 'change preview'):
            apply_update(self.store, expected_revision=None, plan_id=None)
        self.assertEqual(self.store.read(), self.before)

    def test_shared_summary_evidence_stales_the_summary_without_requeueing_its_siblings(self):
        from codemap.store import GraphStore
        graph = deepcopy(self.before)
        directory = next(e for e in graph['entities'] if e['kind'] == 'directory' and e['name'] == 'tools')
        directory['evidence_ids'] = ['ev:move']
        store = GraphStore(Path(self.temp.name)/'shared-summary.sqlite'); store.initialize(graph)
        self.modify()
        payload = canvas_graph(store)
        self.assertEqual(next(e for e in payload['graph']['entities'] if e['id'] == directory['id'])['freshness'], 'stale')
        self.assertEqual(next(e for e in payload['graph']['entities'] if e['id'] == 'fn:display')['freshness'], 'current')
        self.assertNotIn('tools/report.cpp', preview_update(store)['affected_paths'])


if __name__ == '__main__':
    unittest.main()
