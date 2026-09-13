"""Compile concise, source-backed findings into immutable protocol batches."""

from copy import deepcopy

from .core import TABLES, digest, require
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
        require(type(start) is int and type(end) is int and 1 <= start <= end, 'invalid evidence line range')
        require(any(a <= start <= end <= b for a, b in self.ranges.get(self.key(graph, source), [])),
                f"{source['path']}:{start}-{end}: read these lines through blueprint_read or a reading_pack source item on this connection before preparing new evidence")

    def require_whole(self, graph, source):
        total = self.ranges.get((self.key(graph, source), 'total'))
        require(total is not None, f"{source['path']}: read the entire file before marking it read/indexed")
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
    existing = {table: {row['id']: row for row in graph[table]} for table in TABLES}
    for table, rows in structural_defaults.items():
        existing[table].update(rows)
    aliases, resolved = {}, {}
    for table, rows in groups.items():
        require(isinstance(rows, list), f'{table}: expected an array of changes')
        for index, change in enumerate(rows):
            require(isinstance(change, dict), f'{table}: expected a record')
            key = change.get('key')
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
            if key is not None:
                aliases[key] = row_id
            resolved[(table, index)] = row_id

    def reference(value):
        if isinstance(value, list):
            return [reference(item) for item in value]
        if isinstance(value, str) and value.startswith('$'):
            require(value[1:] in aliases, f'unknown local reference: {value}')
            return aliases[value[1:]]
        return value

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
                require('read_state' in change and 'symbols_complete' in change, 'source updates require explicit read_state and symbols_complete')
                if (row['read_state'] == 'read' and previous['read_state'] != 'read') or (row['symbols_complete'] and not previous['symbols_complete']):
                    ledger.require_whole(graph, previous)
            elif table == 'evidence':
                source = source_record(graph, change.get('source', row.get('source_id')))
                require('source_id' not in row or row['source_id'] == source['id'], 'evidence source mismatch')
                require('sha256' not in change or change['sha256'] == source['sha256'], 'evidence fingerprint mismatch')
                ledger.require_range(graph, source, row.get('start_line'), row.get('end_line'))
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
    validate_result(store, batch)
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
