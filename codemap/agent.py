"""Agent-facing operations shared by the stdio adapter and integration tests."""

from pathlib import Path
from threading import Thread
import time

from .core import CATALOG, digest, require
from .agent_views import status_view, claim_view, saved_context
from .preparation import ReadLedger, prepare, prepared_view
from .project import initialize_project, open_project, status, claim_next, commit_result, export_graph, preview_update, apply_update
from .repository import read_source, search_sources
from .server import create_server


def string(description):
    return {'type': 'string', 'minLength': 1, 'description': description}


def integer(default, minimum, maximum):
    return {'type': 'integer', 'default': default, 'minimum': minimum, 'maximum': maximum}


PROJECT = string('Absolute directory containing map.sqlite, returned by blueprint_init.')
DETAIL = {'type': 'string', 'enum': ['summary', 'full'], 'default': 'summary',
          'description': 'Default summary bounds arrays and omits large record bodies. Full preserves the complete response.'}
TABLES = ['sources', 'entities', 'memberships', 'contexts', 'ports', 'flows', 'relations', 'evidence', 'tasks']


def tool(name, description, properties, required, *, read_only=False, idempotent=False):
    return {'name': 'blueprint_' + name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only, 'destructiveHint': False,
                            'idempotentHint': idempotent or read_only, 'openWorldHint': False}}


TOOLS = [
    tool('init', 'Inventory an entire local repository and create a durable map. This does not analyze code. Reopens an existing map only for the same repository and exclusions.',
         {'root': string('Absolute repository directory.'), 'output': string('Absolute new map directory. Default: root/.codemap.'),
          'exclude': {'type': 'array', 'items': string('Explicit exclusion pattern.'), 'default': []}, 'detail': DETAIL}, ['root'], idempotent=True),
    tool('status', 'Read coverage, task state and revision. Check the source snapshot before resuming or delivering. A queue is not a running AI.',
         {'project': PROJECT, 'check_snapshot': {'type': 'boolean', 'default': True}, 'detail': DETAIL}, ['project'], read_only=True),
    tool('update', 'Preview classified file changes, rename matches and recorded dependency impact; apply the exact preview to archive the graph and queue rereading. History lists snapshots, compare returns differences and restore_id, restore creates a new graph revision without changing source files. Preserve layouts and identities. No AI is started. Resolve scan gaps before applying and pause active readers before restoring.',
         {'project': PROJECT, 'action': {'type': 'string', 'enum': ['preview', 'apply', 'history', 'compare', 'restore'], 'default': 'preview'},
          'expected_revision': {'type': 'integer', 'minimum': 0}, 'plan_id': string('Exact plan_id returned by preview, required for apply.'),
          'revision': {'type': 'integer', 'minimum': 0}, 'restore_id': string('Exact restore_id from compare; restore creates a new graph revision without changing repository files.'),
          'renames': {'type': 'array', 'items': {'type': 'object', 'properties': {'from': string('Removed relative path.'), 'to': string('Added relative path.')}, 'required': ['from', 'to'], 'additionalProperties': False}}}, ['project'], idempotent=True),
    tool('next', 'Claim one ready task as the actual reader. Returns an exact batch_template and the first module reading_pack page: actual source, outline, related-file candidates, saved findings and open tasks. Follow its cursor with blueprint_pack; do not reread delivered lines. Read and submit actual findings. Does not start another agent.',
         {'project': PROJECT, 'worker': string('Actual reader identity.'), 'task_id': string('Optional specific ready task.'),
          'lease_seconds': integer(1800, 1, 3600), 'include_pack': {'type': 'boolean', 'default': True}, 'detail': DETAIL}, ['project', 'worker']),
    tool('pack', 'Read a bounded module reading pack with real numbered source, declaration outlines, saved full findings/evidence and pending tasks. Select exactly one task_id, source or entity_id (containers include descendants). Follow next_cursor with the same selector. Related files are candidate context, not extra claimed tasks; their excerpts may be incomplete. Only returned complete source lines count for prepare on this connection. No AI, task claims or review progress changes.',
         {'project': PROJECT, 'task_id': string('Existing task ID, typically returned by next.'),
          'source': string('One inventory source ID or relative path.'), 'entity_id': string('Saved entity or indexed symbol ID; containers expand across their member files.'),
          'cursor': string('Exact next_cursor returned by the previous pack page.'),
          'max_chars': integer(28000, 6000, 64000), 'related_limit': integer(3, 0, 8)}, ['project'], read_only=True),
    tool('read', 'Read numbered source lines with exact fingerprint. Follow next_start to cover the whole file. Reading does not change progress.',
         {'project': PROJECT, 'source': string('Inventory source ID or repository-relative path.'),
          'start': integer(1, 1, 100000000), 'limit': integer(160, 1, 500)}, ['project', 'source'], read_only=True),
    tool('index', 'Build or query the cached local syntax index (Python/C++/C#/TS/JS). Build is paginated by file and changes no review progress or leases. Follow next_offset with snapshot_id to cover the repository. Query returns symbols, unresolved syntax sites or diagnostics for one source. Use symbol_id in prepare for structural fields and source ranges; actual source reading is still required.',
         {'project': PROJECT, 'action': {'type': 'string', 'enum': ['build', 'query', 'status'], 'default': 'query'},
          'source': string('Inventory source ID or relative path; required for query.'),
          'table': {'type': 'string', 'enum': ['symbols', 'sites', 'diagnostics'], 'default': 'symbols'},
          'offset': integer(0, 0, 100000000), 'limit': integer(30, 1, 100),
          'index_id': string('Query cursor identity from the preceding page.'),
          'snapshot_id': string('Build cursor identity from the preceding page.')}, ['project'], idempotent=True),
    tool('search', 'Literal case-insensitive source search. Check truncated and failures; no matches is not proof of no references.',
         {'project': PROJECT, 'query': string('Literal search text.'), 'paths': string('Repository-relative glob; default *.'),
          'limit': integer(100, 1, 500)}, ['project', 'query'], read_only=True),
    tool('query', 'Read saved graph records for cross-file review, evidence, and resumption. Optional ids selects exact IDs; filter matches top-level fields (including membership in array fields). Page at one revision.',
         {'project': PROJECT, 'table': {'type': 'string', 'enum': TABLES},
          'ids': {'type': 'array', 'items': string('Exact record ID.')}, 'filter': {'type': 'object'},
          'offset': integer(0, 0, 100000000), 'limit': integer(50, 1, 200),
          'revision': {'type': 'integer', 'minimum': 0}}, ['project', 'table'], read_only=True),
    tool('context', 'Get a bounded page of existing symbol responsibilities and check their recorded evidence against disk before reuse. Does not mark code read. Query full details by ID; inspect source for new claims or changed interfaces.',
         {'project': PROJECT, 'source': string('Source ID or relative path.'), 'entity_id': string('Specific saved entity ID.'),
          'offset': integer(0, 0, 100000000), 'limit': integer(20, 1, 50),
          'revision': {'type': 'integer', 'minimum': 0}}, ['project'], read_only=True),
    tool('prepare', 'Build and validate an immutable draft without recording analysis progress. Read guide topic preparation. Supply an empty batch_template from next, concise upserts and explicit result/reason. IDs, source hashes and existing record fields are filled by the tool. New evidence requires lines read on this MCP connection. Or supply prepared_id to inspect a saved draft.',
         {'project': PROJECT, 'batch_template': {'type': 'object'}, 'upserts': {'type': 'object'},
          'result': {'type': 'string', 'enum': ['partial', 'done', 'blocked']}, 'reason': string('Actual findings and remaining work.'),
          'new_tasks': {'type': 'array', 'items': {'type': 'object'}}, 'deletes': {'type': 'object'},
          'prepared_id': string('Saved draft to inspect instead of creating one.'), 'detail': DETAIL}, ['project'], idempotent=True),
    tool('commit', 'Atomically validate and save a structured batch from blueprint_next. Preserve exact batch ID and payload when retrying an uncertain response. Never mark unread code complete.',
         {'project': PROJECT, 'batch': {'type': 'object', 'description': 'Full batch from next, with complete upsert records. Read blueprint_guide for record schemas.'},
          'prepared_id': string('Commit the exact persisted draft from blueprint_prepare instead of resending its JSON.'), 'detail': DETAIL},
         ['project'], idempotent=True),
    tool('task', 'Manage saved reading work. renew extends a current lease; retry requeues a blocked task after its cause is resolved; recover requeues expired leases. pause/resume control claims, not AI execution. Return revision must be used in later batches.',
         {'project': PROJECT, 'action': {'type': 'string', 'enum': ['renew', 'retry', 'recover', 'pause', 'resume']},
          'task_id': string('Required for renew/retry.'), 'lease_id': string('Required for renew.'),
          'lease_seconds': integer(1800, 1, 3600), 'detail': DETAIL}, ['project', 'action']),
    tool('export', 'Export saved graph JSON to a new absolute filename. Does not overwrite existing files.',
         {'project': PROJECT, 'output': string('Absolute new export filename.')}, ['project', 'output']),
    tool('canvas', 'Start or reuse a loopback canvas for a map and return its URL for the host to open. No AI is started. The server lives until this MCP connection closes; saved analysis and layout survive. close stops only this connection\'s canvas.',
         {'project': PROJECT, 'action': {'type': 'string', 'enum': ['open', 'close'], 'default': 'open'},
          'port': integer(0, 0, 65535)}, ['project'], idempotent=True),
    tool('guide', 'Read workflow for the portable reading Skill, then the needed graph schema, task protocol, tool guide, or node catalog. No host-specific Skill installation is required. Text guides are paginated.',
         {'topic': {'type': 'string', 'enum': ['workflow', 'graph-format', 'task-protocol', 'commands', 'preparation', 'structure', 'reading-pack', 'catalog']},
          'start': integer(1, 1, 100000000), 'limit': integer(160, 1, 500)}, ['topic'], read_only=True),
]


def validate_input(value, schema, path='arguments'):
    """Validate the deliberately small JSON-schema subset used by our tool catalog."""
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if kind in types:
        require(type(value) is types[kind], f'{path}: expected {kind}')
    if 'enum' in schema:
        require(value in schema['enum'], f'{path}: unsupported value')
    if kind == 'object':
        props = schema.get('properties', {})
        require(all(k in value for k in schema.get('required', [])), f'{path}: missing required field')
        if schema.get('additionalProperties') is False:
            require(not set(value) - props.keys(), f'{path}: unknown field')
        for key, item in value.items():
            if key in props:
                validate_input(item, props[key], path + '.' + key)
    elif kind == 'array':
        for index, item in enumerate(value):
            validate_input(item, schema.get('items', {}), f'{path}[{index}]')
    elif kind == 'string':
        require(len(value.strip()) >= schema.get('minLength', 0), f'{path}: empty text')
    elif kind == 'integer':
        require(schema.get('minimum', value) <= value <= schema.get('maximum', value), f'{path}: out of range')


def absolute(value):
    path = Path(value).expanduser()
    require(path.is_absolute(), 'Use an absolute filesystem path.')
    return path.resolve()


class AgentTools:
    def __init__(self):
        self.canvases = {}
        self.reads = ReadLedger()

    def close(self):
        for server, thread in self.canvases.values():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.canvases.clear()

    def call(self, name, args):
        spec = next((t for t in TOOLS if t['name'] == name), None)
        require(spec is not None, 'Unknown blueprint tool.')
        validate_input(args, spec['inputSchema'])
        name = name.removeprefix('blueprint_')
        if name == 'guide':
            if args['topic'] == 'catalog':
                return {'catalog': CATALOG}
            names = {'graph-format': 'GRAPH_FORMAT.md', 'task-protocol': 'TASK_PROTOCOL.md'}
            root = Path(__file__).resolve().parent.parent
            if args['topic'] == 'workflow':
                base = root if (root / 'skills').is_dir() else root.parent
                path = base / 'skills/repository-blueprint/SKILL.md'
            elif args['topic'] in {'commands', 'preparation', 'structure', 'reading-pack'}:
                path = root / 'skills/repository-blueprint/references' / (args['topic'] + '.md')
            else:
                path = root / names[args['topic']]
            # The installed runtime is self-contained, including its Skill and references.
            if not path.is_file():
                path = root.parent / 'skills/repository-blueprint/references' / (args['topic'] + '.md')
            lines = path.read_text(encoding='utf-8').splitlines()
            start, limit = args.get('start', 1), args.get('limit', 160)
            require(start <= max(1, len(lines)), 'Guide start exceeds length.')
            end = min(len(lines), start - 1 + limit)
            return {'topic': args['topic'], 'version': digest(lines), 'start': start, 'total_lines': len(lines),
                    'text': '\n'.join(lines[start - 1:end]), 'next_start': end + 1 if end < len(lines) else None}
        if name == 'init':
            root = absolute(args['root'])
            output = absolute(args['output']) if 'output' in args else root / '.codemap'
            if (output / 'map.sqlite').is_file():
                store = open_project(output)
                graph = store.read()
                require(absolute(graph['project']['source_root']) == root, 'Existing map belongs to a different repository.')
                if 'exclude' in args:
                    patterns = list(args['exclude'])
                    if output.is_relative_to(root):
                        patterns.append(output.relative_to(root).as_posix())
                    require(set(patterns) == set(graph['inventory']['exclusion_patterns']), 'Existing map has different exclusions; choose a new output.')
                reopened = True
            else:
                store = initialize_project(root, output, excludes=args.get('exclude', []))
                reopened = False
            return {'project_directory': str(output), 'reopened': reopened, **status_view(status(store, check_snapshot=True), args.get('detail', 'summary'))}
        directory = absolute(args['project'])
        store = open_project(directory)
        if name == 'index':
            from .structure import build_index, query_index, index_status
            from .syntax import capabilities
            action = args.get('action', 'query')
            if action == 'status':
                return {**index_status(store), 'backends': capabilities()}
            if action == 'build':
                return build_index(store, **{k: v for k, v in args.items() if k in {'source', 'offset', 'limit', 'snapshot_id'}})
            require('source' in args, 'index query requires source')
            return query_index(store, **{k: v for k, v in args.items() if k in {'source', 'table', 'offset', 'limit', 'index_id'}})
        if name == 'status':
            return status_view(status(store, check_snapshot=args.get('check_snapshot', True)), args.get('detail', 'summary'))
        if name == 'update':
            action = args.get('action', 'preview')
            if action == 'history':
                return {'snapshots': store.snapshot_history()}
            if action in {'compare', 'restore'}:
                from .history import restore_preview, restore_snapshot
                require('revision' in args, 'history action requires revision')
                if action == 'compare': return restore_preview(store, args['revision'])
                require('expected_revision' in args and 'restore_id' in args, 'restore requires a comparison identity')
                return restore_snapshot(store, args['revision'], expected_revision=args['expected_revision'], restore_id=args['restore_id'])
            if action == 'preview':
                return preview_update(store, renames=args.get('renames', ()))
            require('expected_revision' in args and 'plan_id' in args, 'apply requires the preview revision and plan_id')
            return apply_update(store, expected_revision=args['expected_revision'], plan_id=args['plan_id'], renames=args.get('renames', ()))
        if name == 'next':
            claim = claim_view(claim_next(store, args['worker'], task_id=args.get('task_id'), lease_seconds=args.get('lease_seconds', 1800)), args.get('detail', 'summary'))
            if claim['state'] == 'claimed' and args.get('include_pack', True):
                from .reading_pack import reading_pack
                try:
                    claim['reading_pack'] = reading_pack(store, task_id=claim['task']['id'], ledger=self.reads,
                                                        expected_revision=claim['project']['revision'])
                except (OSError, ValueError) as error:
                    # Preserve a successful claim and its exact lease/template on an unavailable source or changed revision.
                    claim['reading_pack'] = {'state': 'unavailable', 'reason': str(error), 'retry': {'tool': 'blueprint_pack', 'task_id': claim['task']['id']}}
            return claim
        if name == 'pack':
            from .reading_pack import reading_pack
            return reading_pack(store, ledger=self.reads, **{k: v for k, v in args.items() if k != 'project'})
        if name == 'prepare':
            if 'prepared_id' in args:
                require(not set(args) - {'project', 'prepared_id', 'detail'}, 'inspect takes only project, prepared_id and detail')
                return prepared_view(args['prepared_id'], store.read_prepared(args['prepared_id']), detail=args.get('detail', 'summary'))
            require(all(key in args for key in ('batch_template', 'upserts', 'result', 'reason')), 'prepare requires batch_template, upserts, result and reason')
            return prepare(store, self.reads, **{key: value for key, value in args.items() if key != 'project'})
        if name == 'commit':
            require(('batch' in args) != ('prepared_id' in args), 'commit requires exactly one of batch or prepared_id')
            batch = store.read_prepared(args['prepared_id']) if 'prepared_id' in args else args['batch']
            committed = commit_result(store, batch)
            return {**status_view(status(store), args.get('detail', 'summary')),
                    'receipt': {'batch_id': batch['batch_id'], **committed['receipts'][batch['batch_id']]}}
        if name == 'export':
            return {'path': export_graph(store, absolute(args['output']))}
        if name == 'canvas':
            action = args.get('action', 'open')
            if action == 'close':
                entry = self.canvases.pop(directory, None)
                if entry:
                    server, thread = entry
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
                return {'closed': bool(entry), 'project_directory': str(directory)}
            reused = directory in self.canvases
            if not reused:
                server = create_server(store, port=args.get('port', 0))
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self.canvases[directory] = (server, thread)
            server, _ = self.canvases[directory]
            return {'url': f'http://127.0.0.1:{server.server_port}/', 'project_directory': str(directory),
                    'reused': reused, 'lifetime': 'mcp_connection', 'ai_started': False}
        graph = store.read()
        if name == 'read':
            source, start = args['source'], args.get('start', 1)
            if source.startswith('symbol:'):
                from .structure import lookup_symbol
                row, symbol = lookup_symbol(store, graph, source)
                source, start = row['source_id'], args.get('start', symbol['start_line'])
            result = read_source(graph, graph['project']['source_root'], source, start=start, limit=args.get('limit', 160))
            self.reads.record(graph, result)
            return result
        if name == 'context':
            context_args = {key: value for key, value in args.items() if key != 'project'}
            if args.get('entity_id', '').startswith('symbol:') and not any(e['id'] == args['entity_id'] for e in graph['entities']):
                from .structure import lookup_symbol, project_structure
                row, _ = lookup_symbol(store, graph, args['entity_id'])
                graph = project_structure(graph, {row['source_id']: row})
                context_args['entity_id'] = next(e['id'] for e in graph['entities'] if e.get('structure', {}).get('id') == args['entity_id'])
            return saved_context(graph, **context_args)
        if name == 'search':
            return search_sources(graph, graph['project']['source_root'], args['query'], paths=args.get('paths', '*'), limit=args.get('limit', 100))
        if name == 'query':
            revision = graph['project']['revision']
            require(args.get('revision', revision) == revision, 'Graph revision changed; restart pagination at offset 0.')
            rows = graph[args['table']]
            if 'ids' in args:
                ids = set(args['ids'])
                rows = [row for row in rows if row.get('id') in ids]
            for key, value in args.get('filter', {}).items():
                rows = [row for row in rows if key in row and
                        (value in row[key] if isinstance(row[key], list) else row[key] == value)]
            offset, limit = args.get('offset', 0), args.get('limit', 50)
            end = offset + limit
            return {'revision': revision, 'table': args['table'], 'records': rows[offset:end], 'total': len(rows),
                    'next_offset': end if end < len(rows) else None}
        action = args['action']
        revision = graph['project']['revision']
        if action == 'renew':
            require('task_id' in args and 'lease_id' in args, 'renew requires task_id and lease_id')
            store.renew(args['task_id'], args['lease_id'], expected_revision=revision, now=time.time(), lease_seconds=args.get('lease_seconds', 1800))
        elif action == 'retry':
            require('task_id' in args, 'retry requires task_id')
            store.retry(args['task_id'], expected_revision=revision)
        elif action == 'recover':
            store.recover(expected_revision=revision, now=time.time())
        else:
            store.set_paused(action == 'pause', expected_revision=revision)
        return status_view(status(store), args.get('detail', 'summary'))
