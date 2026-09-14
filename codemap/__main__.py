"""Local repository inventory, evidence reader, task bridge and canvas CLI."""

import argparse
import json
from pathlib import Path
import sys
import time

from .core import ProtocolError, completion_report, validate_graph
from .project import initialize_project, open_project, status, claim_next, commit_result, export_graph, preview_update, apply_update
from .repository import read_source, search_sources


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='代码蓝图：全仓库清单、源码依据、任务与本地画布。')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('mcp', help='以标准输入输出提供 Agent 工具')
    connect = commands.add_parser('connect', help='输出 MCP 接入配置，不改写宿主设置')
    connect.add_argument('--client', choices=['generic', 'claude-code', 'gemini-cli'], default='generic')
    for name in ('reader-worker', 'reader-mcp'):
        reader = commands.add_parser(name, help='画布阅读进程')
        reader.add_argument('project', type=Path)
        if name == 'reader-mcp':
            reader.add_argument('request_id')
            reader.add_argument('run_token')
    init = commands.add_parser('init', help='扫描完整仓库并建立地图工程')
    init.add_argument('root', type=Path)
    init.add_argument('--output', type=Path)
    init.add_argument('--exclude', action='append', default=[], help='明确排除的相对路径或通配符，可重复')
    init.add_argument('--budget', type=int, default=0)
    init.add_argument('--langs', help='Comma-separated languages')
    init.add_argument('--include', action='append')
    for name in ('sync', 'doctor', 'reindex'):
        sub = commands.add_parser(name)
        sub.add_argument('project', type=Path)
        if name == 'sync':
            sub.add_argument('--budget', type=int, default=0)
    validate = commands.add_parser('validate', help='校验 JSON 交换文件')
    validate.add_argument('graph', type=Path)
    validate.add_argument('--source-root', type=Path)
    for name in ('status', 'metrics', 'index', 'pack', 'update', 'read', 'search', 'next', 'commit', 'renew', 'retry', 'recover', 'pause', 'resume', 'export', 'serve'):
        sub = commands.add_parser(name)
        sub.add_argument('project', type=Path, help='包含 map.sqlite 的工程目录')
        if name == 'status':
            sub.add_argument('--check-snapshot', action='store_true')
        elif name == 'metrics':
            sub.add_argument('--session')
            sub.add_argument('--limit', type=int, default=40)
        elif name == 'index':
            sub.add_argument('--source')
            sub.add_argument('--query', action='store_true')
            sub.add_argument('--offset', type=int, default=0)
            sub.add_argument('--limit', type=int, default=30)
            sub.add_argument('--table', choices=['symbols', 'sites', 'diagnostics'], default='symbols')
        elif name == 'pack':
            selection = sub.add_mutually_exclusive_group(required=True)
            selection.add_argument('--source')
            selection.add_argument('--task-id')
            selection.add_argument('--entity-id')
            sub.add_argument('--cursor')
            sub.add_argument('--max-chars', type=int, default=28000)
            sub.add_argument('--related-limit', type=int, default=3)
        elif name == 'update':
            sub.add_argument('--apply', action='store_true')
            sub.add_argument('--expected-revision', type=int)
            sub.add_argument('--plan-id')
        elif name == 'read':
            sub.add_argument('source', help='清单中的相对路径或来源 ID')
            sub.add_argument('--start', type=int, default=1)
            sub.add_argument('--limit', type=int, default=160)
        elif name == 'search':
            sub.add_argument('query')
            sub.add_argument('--paths', default='*')
            sub.add_argument('--limit', type=int, default=100)
        elif name == 'next':
            sub.add_argument('--worker', required=True, help='实际领取任务的阅读者身份')
            sub.add_argument('--task')
            sub.add_argument('--lease-seconds', type=int, default=1800)
            sub.add_argument('--no-pack', action='store_true')
        elif name == 'commit':
            sub.add_argument('batch', type=Path)
        elif name in {'renew', 'retry'}:
            sub.add_argument('task')
            if name == 'renew':
                sub.add_argument('lease')
                sub.add_argument('--lease-seconds', type=int, default=1800)
        elif name == 'export':
            sub.add_argument('output', type=Path)
        elif name == 'serve':
            sub.add_argument('--port', type=int, default=0)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in commands.choices and not argv[0].startswith('-'):
        argv.insert(0, 'validate')
    args = parser.parse_args(argv)
    if args.command == 'reader-worker':
        from .reader_requests import worker_main
        return worker_main(args.project)
    if args.command == 'reader-mcp':
        from .reader_requests import reader_mcp
        return reader_mcp(args.project, args.request_id, args.run_token)
    if args.command == 'mcp':
        from .mcp import main as mcp_main
        return mcp_main()
    try:
        if args.command == 'connect':
            from .connections import mcp_config
            result = mcp_config(args.client)
        elif args.command == 'validate':
            graph = json.loads(args.graph.read_text(encoding='utf-8-sig'))
            validate_graph(graph, args.source_root)
            result = completion_report(graph)
        elif args.command == 'init':
            output = args.output or args.root / '.codemap'
            if (output / 'map.sqlite').is_file():
                store = open_project(output)
                if Path(store.read()['project']['source_root']).resolve() != args.root.resolve():
                    raise ValueError('Existing map belongs to a different repository')
                if args.exclude:
                    patterns = list(args.exclude)
                    if output.resolve().is_relative_to(args.root.resolve()):
                        patterns.append(output.resolve().relative_to(args.root.resolve()).as_posix())
                    if set(patterns) != set(store.read()['inventory']['exclusion_patterns']):
                        raise ValueError('Existing map has different exclusions; choose a new output')
                from .skeleton import sync
                sync(store, budget=args.budget, langs=args.langs.split(',') if args.langs else None, include=args.include)
            else:
                store = initialize_project(args.root, args.output, excludes=args.exclude, budget=args.budget, langs=args.langs.split(',') if args.langs else None, include=args.include)
            result = {'project_directory': str(Path(store.path).parent), **status(store)}
        else:
            store = open_project(args.project)
            graph = store.read()
            if args.command in {'sync', 'doctor', 'reindex'}:
                from .skeleton import sync, doctor, reindex
                result = sync(store, budget=args.budget) if args.command == 'sync' else reindex(store) if args.command == 'reindex' else doctor(store)
            elif args.command == 'status':
                result = status(store, check_snapshot=args.check_snapshot)
            elif args.command == 'metrics':
                from .metrics import report
                result = report(store, session=args.session, limit=args.limit)
            elif args.command == 'index':
                from .structure import build_index, query_index, index_status
                if args.query:
                    result = query_index(store, source=args.source, table=args.table, offset=args.offset, limit=args.limit)
                else:
                    offset, snapshot = 0, None
                    totals = {'parsed_files': 0, 'cache_hits': 0, 'elapsed_ms': 0}
                    while True:
                        page = build_index(store, source=args.source, offset=offset, snapshot_id=snapshot)
                        for key in totals:
                            totals[key] += page[key]
                        offset, snapshot = page['next_offset'], page['snapshot_id']
                        if offset is None:
                            break
                    result = {**index_status(store), **totals, 'analysis_progress_changed': False}
            elif args.command == 'pack':
                from .reading_pack import reading_pack
                result = reading_pack(store, task_id=args.task_id, source=args.source, entity_id=args.entity_id,
                                      cursor=args.cursor, max_chars=args.max_chars, related_limit=args.related_limit)
            elif args.command == 'update':
                result = apply_update(store, expected_revision=args.expected_revision, plan_id=args.plan_id) if args.apply else preview_update(store)
            elif args.command == 'read':
                result = read_source(graph, graph['project']['source_root'], args.source, start=args.start, limit=args.limit)
            elif args.command == 'search':
                result = search_sources(graph, graph['project']['source_root'], args.query, limit=args.limit, paths=args.paths)
            elif args.command == 'next':
                result = claim_next(store, args.worker, task_id=args.task, lease_seconds=args.lease_seconds)
                if result['state'] == 'claimed' and not args.no_pack:
                    from .reading_pack import reading_pack
                    try:
                        result['reading_pack'] = reading_pack(store, task_id=result['task']['id'], expected_revision=result['project']['revision'])
                    except (OSError, ValueError) as error:
                        result['reading_pack'] = {'state': 'unavailable', 'reason': str(error)}
            elif args.command == 'commit':
                batch = json.loads(args.batch.read_text(encoding='utf-8-sig'))
                commit_result(store, batch)
                result = status(store)
            elif args.command == 'renew':
                store.renew(args.task, args.lease, expected_revision=graph['project']['revision'], now=time.time(), lease_seconds=args.lease_seconds)
                result = status(store)
            elif args.command == 'retry':
                store.retry(args.task, expected_revision=graph['project']['revision'])
                result = status(store)
            elif args.command == 'recover':
                store.recover(expected_revision=graph['project']['revision'], now=time.time())
                result = status(store)
            elif args.command in {'pause', 'resume'}:
                store.set_paused(args.command == 'pause', expected_revision=graph['project']['revision'])
                result = status(store)
            elif args.command == 'export':
                result = {'path': export_graph(store, args.output)}
            else:
                from .server import serve
                serve(store, port=args.port)
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == 'doctor' and not result['ok'] else 0
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f'代码蓝图：{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
