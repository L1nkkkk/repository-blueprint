"""Compile concise, source-backed findings into immutable protocol batches."""

from copy import deepcopy

from .core import TABLES, ProtocolError, digest, require
from .diagnostics import Issues, present
from .project import validate_result
from .repository import source_record


class ReadLedger:
    """Ranges actually returned by this connection, keyed by snapshot and hash."""

    def __init__(self):
        self.ranges = {}

    @staticmethod
    def key(graph, source):
        return (graph['project']['id'], graph['project']['snapshot_id'], source['id'], source['sha256'])

    def record(self, graph, result):
        source = source_record(graph, result['source_id'])
        key = self.key(graph, source)
        ranges = self.ranges.get(key, []) + [(result['start_line'], result['end_line'])]
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        self.ranges[key] = merged
        self.ranges[(key, 'total')] = result['total_lines']

    def require_range(self, graph, source, start, end):
        shape = Issues()
        if not (type(start) is int and type(end) is int and 1 <= start <= end):
            shape.add('evidence_line_range', 'start_line/end_line', 'invalid evidence line range',
                      'Use integer line numbers from the source response with 1 <= start_line <= end_line. Correct the range before checking whether reading is missing.',
                      table='sources', row=source, source_paths=[source['path']], expected='integer range, 1 <= start_line <= end_line')
        shape.raise_if_any('source_range', pending=('actual source reading coverage',))
        returned = self.ranges.get(self.key(graph, source), [])
        missing, cursor = [], start
        for a, b in returned:
            if b < cursor or a > end:
                continue
            if a > cursor:
                missing.append([cursor, min(a - 1, end)])
            cursor = max(cursor, b + 1)
        if cursor <= end:
            missing.append([cursor, end])
        issues = Issues()
        if missing:
            issues.add('source_lines_not_read', 'start_line/end_line', 'read these lines on this connection before preparing new evidence',
                       'Read only missing_ranges through blueprint_read (up to 500 lines per call), or continue the reading pack. Keep this MCP connection; previously returned current lines need no reread.',
                       category='evidence', table='sources', row=source, source_paths=[source['path']],
                       expected={'start_line': start, 'end_line': end}, missing_ranges=missing[:10],
                       missing_range_count=len(missing), requires_read=True)
        issues.raise_if_any('source_reading')

    def require_whole(self, graph, source):
        total = self.ranges.get((self.key(graph, source), 'total'))
        issues = Issues()
        if total is None:
            issues.add('source_not_read_this_connection', 'read_state/symbols_complete', 'read the entire file before marking it read/indexed',
                       'No complete source response is recorded for this file on this MCP connection. Use blueprint_read or blueprint_pack, follow remaining pages, and retain the connection.',
                       category='evidence', table='sources', row=source, source_paths=[source['path']], requires_read=True)
        issues.raise_if_any('source_reading')
        self.require_range(graph, source, 1, total)


REFERENCE_FIELDS = {'source_id', 'source_ids', 'evidence_ids', 'parent_id', 'child_id', 'caller_id', 'callee_id',
                    'callsite_evidence_id', 'entity_id', 'context_id', 'producer_port_id', 'derived_from',
                    'from_id', 'to_id', 'flow_id', 'scope_ids', 'depends_on'}


def prepare(store, ledger, *, batch_template, upserts, result, reason, new_tasks=(), deletes=None, detail='summary'):
    graph = store.read()
    require(isinstance(batch_template, dict), 'batch_template must be the template from blueprint_next')
    fields = ('format_version', 'project_id', 'snapshot_id', 'base_revision', 'batch_id', 'task_id', 'lease_id')
    require(all(key in batch_template for key in fields), 'batch_template is missing its identity or lease')
    require(not batch_template.get('upserts') and not batch_template.get('new_tasks') and not batch_template.get('deletes'),
            'pass an empty batch_template; put findings in upserts/new_tasks/deletes')
    batch = {key: batch_template[key] for key in fields}
    batch.update(upserts={}, new_tasks=[], result=result, reason=reason)
    require(isinstance(upserts, dict) and set(upserts) <= set(TABLES) - {'tasks'}, 'unsupported preparation table')
    require(isinstance(new_tasks, (list, tuple)), 'new_tasks must be an array')
    from .structure import expand_findings
    for table in ('entities', 'evidence'):
        require(isinstance(upserts.get(table, []), list) and all(isinstance(row, dict) for row in upserts.get(table, [])),
                f'{table}: expected an array of records')
    upserts, structural_defaults = expand_findings(store, graph, upserts)
    groups = {**upserts, 'tasks': list(new_tasks)}
    shapes = Issues()
    for table, rows in groups.items():
        if not isinstance(rows, list):
            shapes.add('record_array_required', table, 'expected an array of changes', 'Supply an array of record objects.', table=table, expected='array')
        else:
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    shapes.add('record_object_required', f'{table}[{index}]', 'expected a record object', 'Supply an object for this record.', table=table, expected='object')
    shapes.raise_if_any('input_shape', pending=('record identities', 'references', 'findings and evidence', 'completion and source disk checks'))
    existing = {table: {row['id']: row for row in graph[table]} for table in TABLES}
    for table, rows in structural_defaults.items():
        existing[table].update(rows)
    aliases, resolved = {}, {}
    identities = Issues()
    for table, rows in groups.items():
        require(isinstance(rows, list), f'{table}: expected an array of changes')
        for index, change in enumerate(rows):
            require(isinstance(change, dict), f'{table}: expected a record')
            key = change.get('key')
            try:
                require(key is None or (isinstance(key, str) and key and not key.startswith('$') and key not in aliases),
                        'local keys must be nonempty, unique and omit the $ prefix')
                if table == 'sources':
                    row_id = source_record(graph, change.get('source', change.get('id')))['id']
                    require('id' not in change or change['id'] == row_id, 'source identity mismatch')
                elif 'id' in change:
                    row_id = change['id']
                    require(isinstance(row_id, str) and row_id in existing[table], 'use key for a new record, or an existing id for an update')
                else:
                    require(key is not None, f'{table}: a new record requires a local key')
                    row_id = table + ':' + digest([batch['project_id'], batch['task_id'], table, key])[:24]
                    require(row_id not in existing[table], 'this key already created a record; update its saved id')
            except ProtocolError as error:
                identities.add('record_identity', f'{table}[{index}].id/key/source', str(error),
                               'Use the exact existing ID for an update, or a unique key without $ for a new record. Source records must refer to an existing inventory file.',
                               table=table, row=change)
                continue
            if key is not None:
                aliases[key] = row_id
            resolved[(table, index)] = row_id
    identities.raise_if_any('record_identities', pending=('references', 'findings and evidence', 'completion and source disk checks'))

    references = Issues()
    def check_reference(value, field, table, row):
        if isinstance(value, list):
            for index, item in enumerate(value):
                check_reference(item, f'{field}[{index}]', table, row)
        elif isinstance(value, str) and value.startswith('$') and value[1:] not in aliases:
            references.add('unknown_local_reference', field, 'unknown local reference: ' + value[:160],
                           'Define this key in the same draft or reference an exact saved ID. Reuse existing evidence IDs without copying their records.',
                           table=table, row=row)
    for table, rows in groups.items():
        for index, row in enumerate(rows):
            for field in sorted(REFERENCE_FIELDS & row.keys()):
                check_reference(row[field], f'{table}[{index}].{field}', table, row)
    references.raise_if_any('local_references', pending=('findings and evidence', 'completion and source disk checks'))

    def reference(value):
        if isinstance(value, list):
            return [reference(item) for item in value]
        if isinstance(value, str) and value.startswith('$'):
            require(value[1:] in aliases, f'unknown local reference: {value}')
            return aliases[value[1:]]
        return value

    findings = Issues()
    for table, rows in groups.items():
        compiled = []
        for index, change in enumerate(rows):
            row_id = resolved[(table, index)]
            previous = existing[table].get(row_id, {})
            row = deepcopy(previous)
            row.update({key: reference(value) if key in REFERENCE_FIELDS else deepcopy(value)
                        for key, value in change.items() if key not in {'key', 'source'}})
            row['id'] = row_id
            if table == 'sources':
                for field in ('read_state', 'symbols_complete'):
                    if field not in change:
                        findings.add('source_status_required', field, 'source updates require explicit read_state and symbols_complete',
                                     'Supply this status explicitly from the actual review; do not infer it from merely scanning a file.',
                                     current=present(change, field), table=table, row=row, source_paths=[row['path']])
                if (row.get('read_state') == 'read' and previous['read_state'] != 'read') or (row.get('symbols_complete') and not previous['symbols_complete']):
                    try:
                        ledger.require_whole(graph, previous)
                    except ProtocolError as error:
                        findings.extend(error)
            elif table == 'evidence':
                source = source_record(graph, change.get('source', row.get('source_id')))
                for field, matches, message in [('source_id', 'source_id' not in row or row['source_id'] == source['id'], 'evidence source mismatch'),
                                                ('sha256', 'sha256' not in change or change['sha256'] == source['sha256'], 'evidence fingerprint mismatch')]:
                    if not matches:
                        findings.add('evidence_' + field, field, message,
                                     'Use the inventory source identity and current source evidence. Prepare can fill mechanical IDs/hashes; do not override a changed source fingerprint.',
                                     category='source', table=table, row=row, source_paths=[source['path']])
                try:
                    ledger.require_range(graph, source, row.get('start_line'), row.get('end_line'))
                except ProtocolError as error:
                    findings.extend(error)
                row.update(source_id=source['id'], sha256=source['sha256'])
                # A stale proof can become current only after the explicit reread above.
                row['freshness'] = 'current'
            elif table == 'entities':
                row.setdefault('roles', [])
                row.setdefault('analysis', 'partial')
                row.setdefault('freshness', 'current')
                row.setdefault('evidence_ids', [])
                if 'language' not in row and row.get('source_ids'):
                    languages = {source_record(graph, id)['language'] for id in row['source_ids']}
                    row['language'] = next(iter(languages)) if len(languages) == 1 else ''
                row.setdefault('language', '')
            elif table == 'flows':
                row.setdefault('color_key', row_id)
            elif table == 'tasks':
                for key, value in {'state': 'queued', 'attempt': 0, 'lease': None, 'required': True}.items():
                    row.setdefault(key, value)
            compiled.append(row)
        if table == 'tasks':
            batch['new_tasks'] = compiled
        else:
            batch['upserts'][table] = compiled
    if deletes is not None:
        batch['deletes'] = deepcopy(deletes)
    # Same lease, snapshot, evidence, semantic depth and source-disk gates as commit.
    try:
        validate_result(store, batch)
    except ProtocolError as error:
        findings.extend(error)
    # Keep new/overloaded records identifiable before a successful draft has
    # returned its generated-ID aliases. A unique input location is actionable.
    locations = {}
    for (table, index), row_id in resolved.items():
        locations.setdefault((table, row_id[:240]), []).append(index)
    for issue in findings.rows:
        table = issue.get('table')
        matches = locations.get((table, issue.get('record_id')), [])
        if len(matches) == 1:
            index = matches[0]
            issue['input_path'] = f'new_tasks[{index}]' if table == 'tasks' else f'upserts.{table}[{index}]'
            key = groups[table][index].get('key')
            if isinstance(key, str):
                issue['local_key'] = key[:240]
                if len(key) > 240:
                    issue['local_key_truncated'] = True
    findings.raise_if_any('findings_and_completion', pending=('dependent graph/reference/source checks after listed issues are repaired',))
    prepared_id = store.save_prepared(batch)
    return prepared_view(prepared_id, batch, detail=detail, aliases=aliases)


def prepared_view(prepared_id, batch, *, detail='summary', aliases=None):
    result = {'prepared_id': prepared_id, 'batch_id': batch['batch_id'], 'task_id': batch['task_id'],
              'base_revision': batch['base_revision'], 'result': batch['result'],
              'record_counts': {table: len(rows) for table, rows in batch['upserts'].items()},
              'new_task_count': len(batch['new_tasks']), 'committed': False,
              'next': 'blueprint_commit(project=..., prepared_id=...) revalidates and commits this exact draft; use blueprint_prepare(prepared_id=..., detail="full") to inspect it.'}
    if aliases is not None:
        result['aliases'] = dict(list(aliases.items())[:20])
        result['alias_count'] = len(aliases)
    if detail == 'full':
        result['batch'] = batch
    return result
