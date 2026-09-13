"""Loopback-only canvas server. Browsing never starts an AI worker."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlsplit

from .core import CATALOG, require
from .project import canvas_graph, status, preview_update, apply_update
from .repository import read_source

WEB = Path(__file__).with_name('web')


def create_server(store, *, port=0):
    from .change_analysis import snapshot_texts
    from .coverage import coverage_report
    from .history import restore_preview, restore_snapshot
    from .connections import connection_info, handoff
    from .reader_requests import requests, request_by_id, enqueue, control_request, start_worker, connection_status
    # For pre-existing maps, archive only bytes that still match their saved hashes.
    store.save_source_texts(snapshot_texts(store.read()))
    token = token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, code=200, mime='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host = f'127.0.0.1:{self.server.server_port}'
            require(self.headers.get('Host') == host, 'invalid local Host header')
            origin = self.headers.get('Origin')
            require(origin is None or origin == f'http://{host}', 'cross-origin request rejected')
            require(self.headers.get('Sec-Fetch-Site') != 'cross-site', 'cross-site request rejected')

        def do_GET(self):
            try:
                self.local_request()
                request = urlsplit(self.path)
                query = parse_qs(request.query)
                assets = {
                    '/': ('index.html', 'text/html; charset=utf-8'),
                    **{f'/{name}': (name, 'text/javascript; charset=utf-8') for name in
                       ('app.js', 'canvas-model.js', 'topology-model.js', 'workflow-model.js',
                        'panel-layout.js', 'source-highlight.js', 'workbench.js', 'vendor/highlight.min.js')},
                    **{f'/{name}': (name, 'text/css; charset=utf-8') for name in ('style.css', 'reader.css', 'workbench.css')},
                }
                if request.path in assets:
                    name, mime = assets[request.path]
                    self.respond((WEB / name).read_bytes(), mime=mime)
                elif request.path == '/api/graph':
                    self.respond({**canvas_graph(store), 'catalog': CATALOG, 'view': store.read_view('canvas') or {}, 'write_token': token})
                elif request.path == '/api/version':
                    from .structure import index_status
                    self.respond({**status(store), 'structure_version': index_status(store)['version']})
                elif request.path == '/api/changes':
                    self.respond(preview_update(store))
                elif request.path == '/api/coverage':
                    current = canvas_graph(store)
                    self.respond({**coverage_report(current['graph']), 'snapshot': current['snapshot']})
                elif request.path == '/api/agent-connection':
                    self.respond(connection_info(query.get('client', ['generic'])[0]))
                elif request.path == '/api/requests':
                    if query.get('id'):
                        self.respond(request_by_id(store, query['id'][0]))
                    else:
                        rows = requests(store)
                        self.respond({'requests': [{k: r.get(k) for k in ('id', 'kind', 'question', 'state', 'rounds', 'created_at', 'updated_at', 'error', 'stop_requested')} for r in rows],
                                      'connection': connection_status(store)})
                elif request.path == '/api/history':
                    self.respond(restore_preview(store, int(query['revision'][0])) if query.get('revision') else
                                 {'snapshots': store.snapshot_history(), 'current_revision': store.read()['project']['revision']})
                elif request.path == '/api/source':
                    graph = store.read()
                    self.respond(read_source(graph, graph['project']['source_root'], query.get('id', [''])[0], start=int(query.get('start', ['1'])[0]), limit=int(query.get('limit', ['160'])[0]), with_context=query.get('context', ['0'])[0] == '1'))
                else:
                    self.respond({'error': 'Not found'}, 404)
            except (OSError, ValueError, KeyError) as error:
                self.respond({'error': str(error)}, 400)

        def do_POST(self):
            try:
                self.local_request()
                require(self.path in {'/api/view', '/api/index', '/api/changes', '/api/changes/preview', '/api/requests', '/api/history/restore', '/api/handoff'}, 'unknown write endpoint')
                require(self.headers.get('X-Codemap-Token') == token, 'missing canvas write token')
                require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', 'expected JSON')
                length = int(self.headers.get('Content-Length', '0'))
                require(0 < length <= 1024 * 1024, 'view payload exceeds 1 MiB')
                view = json.loads(self.rfile.read(length))
                require(isinstance(view, dict), 'view must be an object')
                if self.path == '/api/index':
                    from .structure import build_index
                    require(set(view) <= {'offset', 'limit', 'snapshot_id'}, 'unexpected index argument')
                    self.respond(build_index(store, **view))
                elif self.path == '/api/handoff':
                    self.respond(handoff(store, view))
                elif self.path == '/api/changes':
                    require({'expected_revision', 'plan_id'} <= set(view) <= {'expected_revision', 'plan_id', 'renames'}, 'expected a change preview identity')
                    self.respond(apply_update(store, expected_revision=view['expected_revision'], plan_id=view['plan_id'], renames=view.get('renames', ())))
                elif self.path == '/api/changes/preview':
                    require(set(view) == {'renames'}, 'expected rename mappings')
                    self.respond(preview_update(store, renames=view['renames']))
                elif self.path == '/api/history/restore':
                    require(set(view) == {'revision', 'expected_revision', 'restore_id'}, 'expected a restore preview identity')
                    self.respond(restore_snapshot(store, view['revision'], expected_revision=view['expected_revision'], restore_id=view['restore_id']))
                elif self.path == '/api/requests':
                    action = view.get('action')
                    if action == 'create':
                        require(set(view) == {'action', 'request'}, 'expected an analysis request')
                        result = enqueue(store, view['request'])
                    else:
                        require(set(view) == {'action', 'id'}, 'expected a request action and id')
                        result = control_request(store, view['id'], action)
                    if result['state'] == 'queued':
                        try: start_worker(store)
                        except (OSError, ValueError) as error: result = {**result, 'connection_error': str(error)}
                    self.respond(result)
                else:
                    store.save_view('canvas', view)
                    self.respond({'saved': True})
            except (OSError, ValueError, KeyError) as error:
                self.respond({'error': str(error)}, 400)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(store, *, port=0):
    server = create_server(store, port=port)
    print(f'Canvas: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
