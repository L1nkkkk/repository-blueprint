from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from codemap.agent import AgentTools
from codemap.core import ProtocolError, validate_graph, completion_report
from codemap.project import open_project, canvas_graph, preview_update, apply_update
from codemap.reader_requests import enqueue, ensure_analysis_task
from codemap.connections import handoff
from codemap.structure import build_index, query_index, index_status
from codemap.syntax import parse, capabilities
from codemap import parser_runner


def syntax(language, text, path='sample'):
    return parse({'id': 'source:test', 'path': path, 'language': language,
                  'sha256': sha256(text.encode()).hexdigest()}, text)


class SyntaxTests(unittest.TestCase):
    def test_python_decorators_nested_scopes_annotations_and_calls(self):
        code = '@decorate\nclass Actor:\n    speed: float = 2\n    async def move(self, x: int, *, dt=1):\n        def integrate(a):\n            return a + dt\n        return integrate(x)\n'
        result = syntax('python', code)
        names = {s['qualified_name']: s for s in result['symbols']}
        self.assertEqual(result['state'], 'parsed')
        self.assertEqual(set(names), {'Actor', 'Actor.speed', 'Actor.move', 'Actor.move.integrate'})
        self.assertEqual(names['Actor']['start_line'], 1)
        self.assertEqual(names['Actor.move']['kind'], 'method')
        self.assertEqual(names['Actor.move.integrate']['kind'], 'function')
        self.assertEqual([p['name'] for p in names['Actor.move']['parameters']], ['self', 'x', 'dt'])
        self.assertEqual(result['sites'][0]['resolution'], 'unresolved')
        shifted = syntax('python', '\n\n' + code.replace('a + dt', 'a * dt'))
        self.assertEqual([s['id'] for s in result['symbols']], [s['id'] for s in shifted['symbols']])
        self.assertEqual(shifted['symbols'][0]['start_line'], 3)

    def test_supported_native_grammars_are_installed_for_this_test_run(self):
        self.assertTrue(all(item['available'] for item in capabilities().values()), 'Run scripts/setup_parsers.py before the full integration suite.')

    def test_cpp_namespaces_templates_overloads_fields_and_out_of_line_method(self):
        code = 'namespace Game {\nstruct V { float x, y; };\nclass A { public: int move(int n); int move(float n); };\n}\nint Game::A::move(int n) { return n; }\ntemplate<typename T>\nT twice(T n) { return n+n; }\nint (*callback)(int);\n'
        result = syntax('cpp', code)
        self.assertEqual(result['state'], 'parsed', result['diagnostics'])
        moves = [s for s in result['symbols'] if s['qualified_name'] == 'Game.A.move']
        self.assertEqual(len(moves), 3)
        self.assertEqual(len({s['id'] for s in moves}), 3)
        self.assertTrue(all(s['kind'] == 'method' for s in moves))
        twice = next(s for s in result['symbols'] if s['name'] == 'twice')
        self.assertEqual(twice['start_line'], 6)
        self.assertIn('template', twice['signature'])
        callback = next(s for s in result['symbols'] if s['name'] == 'callback')
        self.assertEqual(callback['kind'], 'variable')
        self.assertTrue({'Game.V.x', 'Game.V.y'} <= {s['qualified_name'] for s in result['symbols']})

    def test_csharp_file_namespace_interface_record_primary_constructor(self):
        code = 'namespace Game;\npublic interface IActor { int Move(int n); }\npublic record Point(int X,int Y);\nclass A { public int X { get; set; } public A(int n) { X=n; } public int Move(int n) => n; }\n'
        result = syntax('csharp', code)
        self.assertEqual(result['state'], 'parsed', result['diagnostics'])
        names = {s['qualified_name']: s for s in result['symbols']}
        self.assertTrue({'Game.IActor.Move', 'Game.Point', 'Game.A.X', 'Game.A.A', 'Game.A.Move'} <= names.keys())
        self.assertEqual([p['name'] for p in names['Game.Point']['parameters']], ['X', 'Y'])
        self.assertEqual(names['Game']['end_line'], 4)

    def test_tsx_generic_arrow_interface_and_jsx(self):
        code = 'export namespace G { export interface Pos { x:number; move(n:number):void; } }\nexport const run = <T,>(x:T):T => x;\nconst View = () => <div>Hello</div>;\n'
        result = syntax('typescript', code, 'page.tsx')
        self.assertEqual(result['state'], 'parsed', result['diagnostics'])
        names = {s['qualified_name']: s for s in result['symbols']}
        self.assertTrue({'G.Pos', 'G.Pos.x', 'G.Pos.move', 'run', 'View'} <= names.keys())
        self.assertEqual(names['run']['parameters'][0]['name'], 'x')
        js = syntax('javascript', 'export class A { run(x) { return next(x); } }\nconst f = async (x,y=1)=>x+y;\nconst Page = ()=><div/>;', 'page.jsx')
        self.assertEqual(js['state'], 'parsed', js['diagnostics'])
        self.assertEqual({s['name'] for s in js['symbols']}, {'A', 'run', 'f', 'Page'})
        self.assertEqual(js['sites'][0]['text'], 'next')

    def test_errors_missing_dependencies_and_unsupported_are_explicit(self):
        result = syntax('python', 'def broken(\n')
        self.assertEqual(result['state'], 'partial')
        self.assertGreater(result['diagnostic_count'], 0)
        with patch('codemap.syntax.importlib.import_module', side_effect=ImportError('missing backend')):
            missing = syntax('cpp', 'int f() { return 1; }')
        self.assertEqual(missing['state'], 'unavailable')
        self.assertEqual(missing['symbols'], [])
        self.assertEqual(syntax('rust', 'fn f() {}')['state'], 'unsupported')
        broken = syntax('typescript', 'function ok(x: number) { return x; }\nfunction bad( {', 'sample.ts')
        self.assertEqual(broken['state'], 'partial')
        self.assertIn('ok', [s['name'] for s in broken['symbols']])

    def test_real_canvas_javascript_parses_and_native_failure_is_contained(self):
        path = Path(__file__).resolve().parents[1] / 'codemap/web/app.js'
        text = path.read_text(encoding='utf-8')
        source = {'id': 'source:canvas', 'path': 'app.js', 'language': 'javascript', 'sha256': sha256(text.encode()).hexdigest()}
        result = parser_runner.parse(source, text)
        self.assertEqual(result['state'], 'parsed', result['diagnostics'])
        self.assertIn('renderDetails', {s['name'] for s in result['symbols']})
        with patch('codemap.parser_runner.subprocess.run', return_value=subprocess.CompletedProcess([], -1073741819, '', '')):
            failed = parser_runner.parse(source, text)
        self.assertEqual(failed['state'], 'unavailable')
        self.assertFalse(failed['symbols'])
        with patch('codemap.parser_runner.subprocess.run', side_effect=subprocess.TimeoutExpired('parser', 15)):
            timed_out = parser_runner.parse(source, text)
        self.assertIn('15 seconds', timed_out['diagnostics'][0]['message'])


class StructuralIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / '源码'; self.root.mkdir()
        self.code = 'class Box:\n    def value(self, value: int):\n        return value\n'
        (self.root / 'box.py').write_text(self.code, encoding='utf-8')
        (self.root / 'same.py').write_text('def same(x):\n    return x\n', encoding='utf-8')
        self.tools = AgentTools(); self.addCleanup(self.tools.close)
        self.project = self.tools.call('blueprint_init', {'root': str(self.root)})['project_directory']
        self.store = open_project(self.project)

    def call(self, name, **args):
        return self.tools.call('blueprint_' + name, {'project': self.project, **args})

    def test_index_is_cached_paged_bounded_and_never_counts_as_review(self):
        before = self.store.read()
        with patch('codemap.structure.parse', side_effect=AssertionError('unchanged source reparsed')):
            result = build_index(self.store)
        self.assertEqual(result['cache_hits'], 2)
        page = query_index(self.store, source='box.py', limit=1)
        self.assertEqual(page['next_offset'], 1)
        next_page = query_index(self.store, source='box.py', offset=1, index_id=page['index_id'])
        self.assertEqual(next_page['records'][0]['qualified_name'], 'Box.value')
        canvas = canvas_graph(self.store)
        validate_graph(canvas['graph'])
        self.assertTrue(any(e.get('structure') for e in canvas['graph']['entities']))
        self.assertEqual(canvas['status']['coverage'], completion_report(before))
        self.assertEqual(self.store.read(), before)
        self.assertFalse(any(s['symbols_complete'] or s['read_state'] == 'read' for s in before['sources']))
        self.assertFalse(before['relations'])

    def test_changed_file_cannot_reuse_or_promote_old_cache_and_only_it_reparses(self):
        before = query_index(self.store, source='box.py')
        (self.root / 'box.py').write_text('\n' + self.code, encoding='utf-8')
        with self.assertRaises(ProtocolError):
            query_index(self.store, source='box.py')
        self.assertFalse(any(e.get('structure', {}).get('source_id') == before['source_id'] for e in canvas_graph(self.store)['graph']['entities']))
        plan = preview_update(self.store)
        apply_update(self.store, expected_revision=plan['base_revision'], plan_id=plan['plan_id'])
        result = build_index(self.store)
        self.assertEqual(result['parsed_files'], 1)
        self.assertEqual(result['cache_hits'], 1)
        with self.assertRaisesRegex(ProtocolError, 'Index changed'):
            query_index(self.store, source='box.py', index_id=before['index_id'])
        after = query_index(self.store, source='box.py')
        self.assertEqual([r['id'] for r in before['records']], [r['id'] for r in after['records']])
        self.assertNotEqual(before['index_id'], after['index_id'])

    def test_symbol_prepare_reuses_structure_but_requires_real_reads_and_semantics(self):
        claim = self.call('next', worker='index-reader', include_pack=False)
        self.assertEqual(claim['structure']['files'][0]['symbol_count'], 2)
        page = self.call('index', source='box.py')
        klass, fn = page['records']
        changes = {'evidence': [{'key': 'why', 'symbol_id': fn['id'], 'note': 'Returns its value argument.'}],
                   'entities': [{'symbol_id': fn['id'], 'key': 'function', 'analysis': 'reviewed',
                                 'summary': 'Returns the supplied value.', 'evidence_ids': ['$why'],
                                 'details': {'inputs': ['value'], 'outputs': ['value'], 'calls': [], 'reads': [], 'writes': [], 'conditions': []}}]}
        args = dict(batch_template=claim['batch_template'], upserts=changes, result='partial', reason='Class and file review remain.', detail='full')
        before = self.store.read()
        with self.assertRaisesRegex(ProtocolError, 'read these lines'):
            self.call('prepare', **args)
        self.call('read', source=fn['id'])
        draft = self.call('prepare', **args)
        self.assertEqual(self.store.read(), before)
        new = {e['id']: e for e in draft['batch']['upserts']['entities']}
        self.assertEqual(new[fn['id']]['name'], 'value')
        self.assertEqual(new[klass['id']]['analysis'], 'located')
        proof = draft['batch']['upserts']['evidence'][0]
        self.assertEqual((proof['start_line'], proof['end_line']), (2, 3))
        self.assertTrue(draft['batch']['upserts']['memberships'])
        other = AgentTools(); self.addCleanup(other.close)
        other.call('blueprint_commit', {'project': self.project, 'prepared_id': draft['prepared_id']})
        committed = self.store.read()
        self.assertTrue(any(e['id'] == fn['id'] for e in committed['entities']))
        self.assertEqual(sum(e['id'] == fn['id'] for e in canvas_graph(self.store)['graph']['entities']), 1)
        self.assertEqual(index_status(self.store)['symbols'], 3)

    def test_virtual_node_can_be_selected_by_external_and_canvas_agents(self):
        fn = query_index(self.store, source='box.py')['records'][1]
        before = self.store.read()
        result = handoff(self.store, {'kind': 'question', 'question': 'Explain', 'entity_id': fn['id']})
        self.assertIn(fn['id'], result['prompt'])
        self.assertEqual(self.store.read(), before)
        request = enqueue(self.store, {'id': 'index-test', 'kind': 'analyze', 'question': 'Explain', 'entity_id': fn['id']})
        task_id = ensure_analysis_task(self.store, request)
        task = next(t for t in self.store.read()['tasks'] if t['id'] == task_id)
        self.assertTrue(task['source_ids'])
        validate_graph(self.store.read())

    def test_legacy_symbol_is_reused_without_changing_its_identity(self):
        claim = self.call('next', worker='legacy-reader')
        self.call('read', source='box.py')
        proof = {'key': 'why', 'source': 'box.py', 'start_line': 2, 'end_line': 3, 'note': 'Returns value.'}
        entity = {'key': 'old', 'kind': 'method', 'name': 'Box.value', 'source_ids': [claim['sources'][0]['id']],
                  'summary': 'Returns value.', 'analysis': 'reviewed', 'evidence_ids': ['$why'],
                  'details': {'inputs': ['value'], 'outputs': ['value'], 'calls': [], 'reads': [], 'writes': [], 'conditions': []}}
        draft = self.call('prepare', batch_template=claim['batch_template'], upserts={'entities': [entity], 'evidence': [proof]},
                          result='partial', reason='Other review remains.')
        self.call('commit', prepared_id=draft['prepared_id'])
        old_id = draft['aliases']['old']
        symbol = query_index(self.store, source='box.py')['records'][1]
        context = self.call('context', entity_id=symbol['id'])
        self.assertEqual(context['records'][0]['id'], old_id)
        self.assertEqual(context['records'][0]['reuse'], 'current_evidence')
        claim = self.call('next', worker='new-reader')
        revised = self.call('prepare', batch_template=claim['batch_template'],
                            upserts={'entities': [{'symbol_id': symbol['id'], 'summary': 'Returns value unchanged.'}]},
                            result='partial', reason='Other review remains.', detail='full')
        methods = [e for e in revised['batch']['upserts']['entities'] if e['kind'] == 'method']
        self.assertEqual([e['id'] for e in methods], [old_id])

    def test_structure_metadata_cannot_break_the_canvas_when_promoted(self):
        projected = canvas_graph(self.store)['graph']
        row = next(e for e in projected['entities'] if e.get('structure'))
        row['structure']['parameters'] = None
        with self.assertRaisesRegex(ProtocolError, 'structural signature'):
            validate_graph(projected)


if __name__ == '__main__':
    unittest.main()
