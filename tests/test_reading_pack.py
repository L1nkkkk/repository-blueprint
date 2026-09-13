from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codemap.agent import AgentTools
from codemap.core import ProtocolError
from codemap.project import open_project
from codemap.reading_pack import Material, encode_cursor, import_paths, related_sources, size


class ReadingPackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / '源码'
        self.root.mkdir()
        self.tools = AgentTools()
        self.addCleanup(self.tools.close)

    def setup_map(self, files):
        for path, content in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        self.project = self.tools.call('blueprint_init', {'root': str(self.root)})['project_directory']
        self.store = open_project(self.project)

    def call(self, name, **args):
        return self.tools.call('blueprint_' + name, {'project': self.project, **args})

    def pages(self, **args):
        pages, cursor = [], None
        for _ in range(100):
            page = self.call('pack', **args, **({'cursor': cursor} if cursor else {}))
            self.assertLessEqual(size(page), args.get('max_chars', 28000))
            self.assertFalse(page['analysis_progress_changed'])
            pages.append(page)
            if page['next_cursor'] is None:
                return pages
            self.assertNotEqual(page['next_cursor'], cursor)
            cursor = page['next_cursor']
        self.fail('pack cursor did not finish')

    def test_claim_includes_real_material_and_can_prepare_without_duplicate_read(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        claim = self.call('next', worker='reader')
        packet = claim['reading_pack']
        self.assertGreater(packet['delivered_source_lines'], 0)
        source_items = [i for i in packet['items'] if i['kind'] == 'source']
        self.assertEqual(source_items[0]['lines'], [{'number': 1, 'text': 'VALUE = 1'}])
        graph = self.store.read()
        self.assertEqual(graph['sources'][0]['read_state'], 'unread')
        self.assertFalse(graph['sources'][0]['symbols_complete'])
        draft = self.call('prepare', batch_template=claim['batch_template'], result='done', reason='Read the literal module assignment.',
            upserts={'sources': [{'source': 'value.py', 'read_state': 'read', 'symbols_complete': True}],
                     'entities': [{'id': claim['entities'][0]['id'], 'summary': 'Defines VALUE as 1.', 'analysis': 'reviewed', 'evidence_ids': ['$assignment']}],
                     'evidence': [{'key': 'assignment', 'source': 'value.py', 'start_line': 1, 'end_line': 1, 'note': 'Literal assignment.'}]})
        self.assertEqual(self.store.read(), graph)
        result = self.call('commit', prepared_id=draft['prepared_id'])
        self.assertEqual(result['coverage']['sources_read'], 1)

    def test_exact_source_pagination_is_bounded_and_cannot_credit_missing_lines(self):
        content = ''.join(f'# {n}: {"资料" * 40}\n' for n in range(300))
        self.setup_map({'large.py': content})
        before = deepcopy(self.store.read())
        first = self.call('pack', source='large.py', max_chars=6000, related_limit=0)
        self.assertIsNotNone(first['next_cursor'])
        source = before['sources'][0]
        with self.assertRaisesRegex(ProtocolError, 'read these lines'):
            self.tools.reads.require_whole(before, source)
        pages = [first]
        cursor = first['next_cursor']
        while cursor:
            page = self.call('pack', source='large.py', max_chars=6000, related_limit=0, cursor=cursor)
            self.assertLessEqual(size(page), 6000)
            pages.append(page)
            cursor = page['next_cursor']
        lines = [line for page in pages for item in page['items'] if item['kind'] == 'source' for line in item['lines']]
        self.assertEqual([line['number'] for line in lines], list(range(1, 301)))
        self.assertEqual([line['text'] for line in lines], content.splitlines())
        self.tools.reads.require_whole(before, source)
        self.assertEqual(self.store.read(), before)

    def test_cross_file_import_context_is_candidate_bounded_and_not_a_task_claim(self):
        self.setup_map({'feature/entry.py': 'from .store import save\n\ndef run(data):\n    return save(data)\n',
                        'feature/store.py': 'def save(data):\n    return data\n' + '# more\n' * 100,
                        'other.py': 'OTHER = 1\n'})
        before = deepcopy(self.store.read())
        pages = self.pages(source='feature/entry.py', related_limit=1)
        hints = pages[0]['related_files']
        self.assertEqual([h['path'] for h in hints], ['feature/store.py'])
        self.assertEqual(hints[0]['reasons'][0]['resolution'], 'unresolved')
        bodies = [i for page in pages for i in page['items'] if i['kind'] == 'source']
        related = [i for i in bodies if i['role'] == 'related']
        self.assertEqual(sum(len(i['lines']) for i in related), 80)
        self.assertEqual(related[-1]['next_start'], 81)
        target = next(s for s in before['sources'] if s['path'] == 'feature/store.py')
        with self.assertRaisesRegex(ProtocolError, 'read these lines'):
            self.tools.reads.require_whole(before, target)
        self.assertEqual(self.store.read(), before)
        self.assertFalse(any(i['path'] == 'other.py' for i in bodies))

    def test_container_scope_keeps_all_member_files_and_omitted_related_count(self):
        self.setup_map({'part/a.py': 'import x\nimport y\n', 'part/b.py': 'B = 2\n', 'x.py': 'X = 1\n', 'y.py': 'Y = 1\n'})
        graph = self.store.read()
        container = next(e for e in graph['entities'] if e['kind'] == 'directory' and e['name'] == 'part')
        pages = self.pages(entity_id=container['id'], related_limit=1)
        self.assertEqual(pages[0]['scope']['primary_file_count'], 2)
        self.assertEqual(pages[0]['related_discovery']['omitted_file_count'], 1)
        primary = {i['path'] for p in pages for i in p['items'] if i['kind'] == 'source' and i['role'] == 'primary'}
        self.assertEqual(primary, {'part/a.py', 'part/b.py'})

    def test_cursor_rejects_another_scope_revision_and_modified_disk(self):
        self.setup_map({'a.py': '# long\n' * 2000, 'b.py': 'B = 1\n'})
        first = self.call('pack', source='a.py', max_chars=6000, related_limit=0)
        cursor = first['next_cursor']
        self.assertIsNotNone(cursor)
        with self.assertRaisesRegex(ProtocolError, 'changed'):
            self.call('pack', source='b.py', cursor=cursor, related_limit=0)
        (self.root / 'a.py').write_text('# changed\n', encoding='utf-8')
        reads = deepcopy(self.tools.reads.ranges)
        with self.assertRaisesRegex(ProtocolError, 'snapshot'):
            self.call('pack', source='a.py', cursor=cursor, related_limit=0)
        self.assertEqual(self.tools.reads.ranges, reads)
        (self.root / 'a.py').write_text('# long\n' * 2000, encoding='utf-8')
        self.call('next', worker='another-reader', include_pack=False)
        with self.assertRaisesRegex(ProtocolError, 'changed'):
            self.call('pack', source='a.py', cursor=cursor, related_limit=0)

    def test_oversized_line_is_a_gap_and_never_counts_as_a_complete_read(self):
        self.setup_map({'large.py': '#' + 'x' * 10000 + '\nVALUE = 1\n'})
        pages = self.pages(source='large.py', related_limit=0, max_chars=6000)
        gaps = [i for p in pages for i in p['items'] if i['kind'] == 'gap']
        self.assertEqual(gaps[0]['line'], 1)
        self.assertEqual(gaps[0]['read']['limit'], 1)
        graph = self.store.read()
        with self.assertRaises(ProtocolError):
            self.tools.reads.require_whole(graph, graph['sources'][0])
        self.tools.reads.require_range(graph, graph['sources'][0], 2, 2)
        self.call('read', source='large.py', start=1, limit=1)
        self.tools.reads.require_whole(graph, graph['sources'][0])

    def test_saved_complete_findings_are_reusable_but_changed_evidence_is_not(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        claim = self.call('next', worker='first-reader')
        draft = self.call('prepare', batch_template=claim['batch_template'], result='partial', reason='Saved file responsibility; more review remains.',
            upserts={'entities': [{'id': claim['entities'][0]['id'], 'summary': 'Defines VALUE as 1.', 'analysis': 'reviewed', 'evidence_ids': ['$proof']}],
                     'evidence': [{'key': 'proof', 'source': 'value.py', 'start_line': 1, 'end_line': 1, 'note': 'Literal value.'}]})
        self.call('commit', prepared_id=draft['prepared_id'])
        pages = self.pages(source='value.py', related_limit=0)
        rows = [i for p in pages for i in p['items'] if i['kind'] == 'saved_record' and i['table'] == 'entities']
        self.assertTrue(rows[0]['complete_record'])
        self.assertEqual(rows[0]['reuse'], 'current_evidence')
        self.assertEqual(rows[0]['record']['summary'], 'Defines VALUE as 1.')
        (self.root / 'value.py').write_text('VALUE = 2\n', encoding='utf-8')
        page = self.call('pack', source='value.py', related_limit=0)
        self.assertTrue(page['page_has_gaps'])
        self.assertFalse(any(i['kind'] == 'saved_record' and i.get('reuse') == 'current_evidence' for i in page['items']))

    def test_pack_read_ledger_is_not_shared_with_another_connection(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        self.call('pack', source='value.py')
        other = AgentTools()
        self.addCleanup(other.close)
        graph = self.store.read()
        self.tools.reads.require_whole(graph, graph['sources'][0])
        with self.assertRaises(ProtocolError):
            other.reads.require_whole(graph, graph['sources'][0])

    def test_invalid_selectors_cursors_and_budgets_fail_before_recording_reads(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        for args in ({}, {'source': 'value.py', 'task_id': 'task:nope'}, {'source': '../outside.py'},
                     {'source': 'value.py', 'cursor': 'not-json'}, {'source': 'value.py', 'max_chars': 0},
                     {'source': 'value.py', 'related_limit': 9}):
            with self.assertRaises(ValueError):
                self.call('pack', **args)
        first = self.call('pack', source='value.py')
        invalid = encode_cursor({'pack_id': first['pack_id'], 'unit': 999, 'part': 0, 'line': 1})
        before = deepcopy(self.tools.reads.ranges)
        with self.assertRaises(ProtocolError):
            self.call('pack', source='value.py', cursor=invalid)
        self.assertEqual(self.tools.reads.ranges, before)

    def test_cli_pack_and_claim_opt_out_keep_source_and_progress_contract(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        before = self.store.read()
        output = subprocess.check_output([sys.executable, '-B', '-m', 'codemap', 'pack', self.project, '--source', 'value.py'], encoding='utf-8')
        page = json.loads(output)
        self.assertEqual(page['delivered_source_lines'], 1)
        self.assertEqual(self.store.read(), before)
        claim = self.call('next', worker='metadata-only', include_pack=False)
        self.assertNotIn('reading_pack', claim)
        self.assertEqual(self.tools.reads.ranges, {})

    def test_saved_relation_checks_both_endpoints_and_keeps_uncertainty(self):
        self.setup_map({'a.py': 'A = 1\n', 'b.py': 'B = 2\n'})
        graph = self.store.read()
        a, b = graph['sources']
        ea = next(e for e in graph['entities'] if a['id'] in e['source_ids'])
        eb = next(e for e in graph['entities'] if b['id'] in e['source_ids'])
        proof = {'id': 'ev:test', 'source_id': a['id'], 'sha256': a['sha256'], 'start_line': 1, 'end_line': 1, 'note': 'Fixture proof.'}
        graph['evidence'].append(proof)
        relation = {'id': 'relation:test', 'from_id': ea['id'], 'to_id': eb['id'], 'basis': 'unresolved', 'reason': 'Uncertain target.', 'evidence_ids': [proof['id']], 'freshness': 'current'}
        graph['relations'].append(relation)
        material = Material(self.store, graph)
        self.assertEqual(material.reusable(relation, 'relations'), 'needs_review')
        selected, hints, count = related_sources(material, [a], 3)
        self.assertEqual(selected, [b['id']])
        self.assertEqual(hints[0]['reasons'][0]['record_id'], relation['id'])
        relation['basis'] = 'source'
        self.assertEqual(Material(self.store, graph).reusable(relation, 'relations'), 'current_evidence')
        (self.root / 'b.py').write_text('B = 3\n', encoding='utf-8')
        self.assertEqual(Material(self.store, graph).reusable(relation, 'relations'), 'needs_review')

    def test_oversized_saved_record_has_exact_retrieval_pointer(self):
        self.setup_map({'value.py': 'VALUE = 1\n'})
        claim = self.call('next', worker='first-reader')
        full_summary = 'Detailed fixture finding. ' * 700
        draft = self.call('prepare', batch_template=claim['batch_template'], result='partial', reason='Remaining review.',
            upserts={'entities': [{'id': claim['entities'][0]['id'], 'summary': full_summary}]})
        self.call('commit', prepared_id=draft['prepared_id'])
        before = self.store.read()
        pages = self.pages(source='value.py', max_chars=6000, related_limit=0)
        gaps = [i for p in pages for i in p['items'] if i['kind'] == 'metadata_gap']
        self.assertEqual(len(gaps), 1)
        query = gaps[0]['query']
        found = self.call('query', **{k: v for k, v in query.items() if k != 'tool'})
        self.assertEqual(found['records'][0]['summary'], full_summary)
        self.assertEqual(self.store.read(), before)

    def test_missing_parser_does_not_hide_real_source_or_invent_findings(self):
        self.setup_map({'tool.ps1': '$value = 1\n'})
        pages = self.pages(source='tool.ps1')
        file = next(i for p in pages for i in p['items'] if i['kind'] == 'file')
        self.assertEqual(file['structure']['state'], 'unsupported')
        self.assertEqual(file['structure']['symbols'], [])
        self.assertEqual(sum(p['delivered_source_lines'] for p in pages), 1)
        self.assertEqual(self.store.read()['sources'][0]['read_state'], 'unread')

    def test_local_import_path_candidates_support_python_cpp_and_typescript(self):
        self.assertIn('pkg/store.py', import_paths({'path': 'pkg/entry.py', 'language': 'python'}, {'text': 'from . import store'}))
        self.assertIn('store.py', import_paths({'path': '__init__.py', 'language': 'python'}, {'text': 'from . import store'}))
        self.assertIn('src/store.h', import_paths({'path': 'src/main.cpp', 'language': 'cpp'}, {'text': '#include "store.h"'}))
        self.assertIn('src/store.ts', import_paths({'path': 'src/main.ts', 'language': 'typescript'}, {'text': 'import {save} from "./store.js"'}))
        self.assertEqual(import_paths({'path': 'src/main.ts', 'language': 'typescript'}, {'text': 'import x from "external-package"'}), [])

    def test_question_reader_can_pack_its_map_without_claiming_a_task(self):
        from unittest.mock import patch
        from codemap.reader_requests import enqueue, change_request, reader_mcp
        self.setup_map({'value.py': 'VALUE = 1\n'})
        request = enqueue(self.store, {'id': 'pack-question', 'kind': 'question', 'question': 'Explain value.'})
        change_request(self.store, request['id'], lambda r: r.update(state='running', run_token='active'))
        with patch('codemap.mcp.main', side_effect=lambda server: server):
            server = reader_mcp(self.project, request['id'], 'active')
        self.addCleanup(server.tools.close)
        before = self.store.read()
        result = server.tools.call('blueprint_pack', {'project': self.project, 'source': 'value.py'})
        self.assertEqual(result['delivered_source_lines'], 1)
        self.assertEqual(self.store.read(), before)
        with self.assertRaisesRegex(ProtocolError, '不允许'):
            server.tools.call('blueprint_next', {'project': self.project, 'worker': 'not-allowed'})

    def test_pack_failure_preserves_claim_and_revision_guard_records_no_reads(self):
        from unittest.mock import patch
        from codemap.reading_pack import reading_pack
        self.setup_map({'value.py': 'VALUE = 1\n'})
        with patch('codemap.reading_pack.reading_pack', side_effect=OSError('source temporarily unavailable')):
            claim = self.call('next', worker='reader')
        self.assertEqual(claim['state'], 'claimed')
        self.assertEqual(claim['reading_pack']['state'], 'unavailable')
        saved = next(t for t in self.store.read()['tasks'] if t['id'] == claim['task']['id'])
        self.assertEqual(saved['lease']['id'], claim['batch_template']['lease_id'])
        with self.assertRaisesRegex(ProtocolError, 'revision changed'):
            reading_pack(self.store, task_id=saved['id'], ledger=self.tools.reads, expected_revision=0)
        self.assertEqual(self.tools.reads.ranges, {})


if __name__ == '__main__':
    unittest.main()
