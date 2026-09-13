"""Actionable, bounded validation failures. Never repair semantic findings."""

DETAIL_FIELDS = ('inputs', 'outputs', 'calls', 'reads', 'writes', 'conditions')


class ProtocolError(ValueError):
    """An invalid graph, stale request, or unsafe state transition."""


class ValidationErrors(ProtocolError):
    def __init__(self, issues, *, stage, count=None, pending=(), categories=None):
        self.issues = issues
        self.count = len(issues) if count is None else count
        self.stage, self.pending = stage, list(pending)
        self.categories = set(categories) if categories is not None else {i['category'] for i in issues}
        self.category = next(iter(self.categories)) if len(self.categories) == 1 else 'mixed'
        lines = [f"{i.get('record_name') or i.get('record_id') or i.get('table', '')} {i['field']}: {i['message']}" for i in issues]
        super().__init__(f'{self.count} validation issue(s) [{stage}]: ' + '; '.join(lines))

    def report(self):
        return {'error': 'validation_failed', 'stage': self.stage, 'issues': self.issues,
                'issue_count': self.count, 'truncated': self.count > len(self.issues),
                'categories': sorted(self.categories),
                'pending_checks': self.pending,
                'next': 'Fix the listed fields using existing findings. Read source only when an issue requests reading. '
                        'Do not retry an unchanged rejected payload or invent findings to satisfy completion. '
                        'Fields marked *_truncated are previews, not exact selectors; resolve them from the original draft/full records. '
                        'Dependent checks may reveal further issues after these repairs.'}


class Issues:
    """Keep response size bounded while counting all discovered issues."""
    def __init__(self, limit=40):
        self.rows, self.count, self.limit = [], 0, limit
        self.categories = set()

    def add(self, code, field, message, action, *, category='validation', current=None,
            expected=None, table=None, row=None, source_paths=(), **extra):
        self.count += 1
        self.categories.add(category)
        if len(self.rows) >= self.limit:
            return
        item = {'code': code, 'category': category, 'field': field, 'message': message, 'action': action}
        if table is not None:
            item['table'] = table
        if row:
            for old, new in [('id', 'record_id'), ('key', 'local_key'), ('name', 'record_name')]:
                if isinstance(row.get(old), str):
                    item[new] = row[old][:240]
                    if len(row[old]) > 240:
                        item[new + '_truncated'] = True
        if source_paths:
            item['source_paths'] = [str(p)[:300] for p in source_paths[:5]]
            if len(source_paths) > 5 or any(len(str(p)) > 300 for p in source_paths[:5]):
                item['source_paths_truncated'] = True
        if current is not None:
            item['current'] = current
        if expected is not None:
            item['expected'] = expected
        item.update(extra)
        self.rows.append(item)

    def extend(self, error):
        if isinstance(error, ValidationErrors):
            self.count += error.count
            self.categories.update(error.categories)
            self.rows.extend(error.issues[:max(0, self.limit - len(self.rows))])
        else:
            message = str(error)
            category, action = fallback_repair(message)
            self.add('protocol_check_failed', 'batch', message[:500], action, category=category)

    def raise_if_any(self, stage, *, pending=()):
        if self.count:
            raise ValidationErrors(self.rows, stage=stage, count=self.count, pending=pending, categories=self.categories)


def fallback_repair(message):
    """Legacy checks retain their exact message; do not guess missing fields."""
    lower = message.lower()
    if 'lease' in lower:
        return 'lease', 'Inspect the current task and lease. Renew only your own live lease, or recover and reclaim an expired task. Use the current revision; keep saved findings.'
    if 'revision' in lower:
        return 'revision', 'Read the current project revision and task identity; reconcile intervening changes before preparing a new draft. Source rereading is needed only for changed or missing evidence.'
    if any(word in lower for word in ('snapshot', 'fingerprint', 'source changed', 'source is no longer', 'hash')):
        return 'source', 'Check the source snapshot with blueprint_status(check_snapshot=true) and preview blueprint_update. Reconcile actual changes; never replace a saved hash just to bypass this check.'
    return 'validation', 'Correct this protocol requirement using the preparation/graph-format guide. This check alone does not establish that more source reading is needed.'


def present(row, field):
    if field not in row:
        return {'present': False}
    value = row[field]
    types = {str: 'string', list: 'array', dict: 'object', int: 'integer', float: 'number', bool: 'boolean', type(None): 'null'}
    description = {'present': True, 'type': types.get(type(value), type(value).__name__)}
    # Do not echo arbitrary summary/source text or large input objects.
    if value is None or type(value) in (bool, int):
        description['value'] = value
    elif isinstance(value, str) and len(value) <= 80:
        description['value'] = value
    return description


def review_issues(graph, *, task=None, result=None):
    """Independent finding/completion checks, before graph/reference/disk gates.

    Called on a proposed copy; reports never mutate the graph or infer absence.
    Defensive shapes let the authoritative graph validator handle other fields.
    """
    issues = Issues()
    entities = {e['id']: e for e in graph['entities'] if isinstance(e, dict) and isinstance(e.get('id'), str)}
    sources = {s['id']: s for s in graph['sources'] if isinstance(s, dict) and isinstance(s.get('id'), str)}
    def paths(row):
        ids = row.get('source_ids', [])
        return [sources[id].get('path', id) for id in ids if isinstance(id, str) and id in sources] if isinstance(ids, list) else []
    empty_inventory = (not sources and graph.get('project', {}).get('inventory_complete')
                       and graph.get('inventory', {}).get('policy') == 'whole-tree-v1'
                       and not graph.get('inventory', {}).get('gaps'))
    for row in entities.values():
        if row.get('analysis') != 'reviewed':
            continue
        context = {'table': 'entities', 'row': row, 'source_paths': paths(row)}
        if not isinstance(row.get('summary'), str) or not row['summary'].strip():
            issues.add('review_summary_missing', 'summary', 'reviewed entity requires summary and evidence: summary is empty',
                       'Supply the responsibility supported by your findings, or keep analysis=partial while that work remains.',
                       current=present(row, 'summary'), expected='nonempty string', **context)
        proofs = row.get('evidence_ids')
        if not (isinstance(proofs, list) and proofs) and not (row.get('kind') in ('repository', 'directory') and empty_inventory):
            issues.add('review_evidence_missing', 'evidence_ids', 'reviewed entity requires summary and evidence: evidence IDs are missing',
                       'Reference existing current proof IDs when they support this finding. For new evidence, read its exact source lines on this connection.',
                       category='evidence', current=present(row, 'evidence_ids'), expected='nonempty array of supporting evidence IDs', **context)
        if row.get('kind') in ('function', 'method'):
            details = row.get('details')
            if not isinstance(details, dict):
                issues.add('function_details_object', 'details', 'function-level record is incomplete: function details must be an object',
                           'Provide details with inputs, outputs, calls, reads, writes and conditions arrays from your findings. [] means explicitly checked and absent.',
                           current=present(row, 'details'), expected={'type': 'object', 'array_fields': list(DETAIL_FIELDS)}, **context)
            else:
                for field in DETAIL_FIELDS:
                    if not isinstance(details.get(field), list):
                        issues.add('function_details_array', 'details.' + field, 'function-level record is incomplete: this field must be an explicit array',
                                   'Fill this array from the findings already read. Use [] only after verifying absence; missing/null does not mean empty. Preserve the other details fields.',
                                   current=present(details, field), expected='array', **context)
    if task is not None and result == 'done':
        if task['kind'] in ('analyze', 'update') or task.get('review_ids'):
            for id in task['scope_ids']:
                row = entities.get(id)
                if row is None:
                    issues.add('scope_entity_missing', 'scope_ids', 'task scope references a missing entity',
                               'Reconcile this task scope with the saved graph; do not drop required work.', table='tasks', row=task, current=id)
                    continue
                for field, target in [('analysis', 'reviewed'), ('freshness', 'current')]:
                    if row.get(field) != target:
                        issues.add('scope_' + field, field, 'task completion requires this scope node status',
                                   'For this exact scope node (including file/module nodes), supply supported findings and explicit reviewed/current status. If work remains, submit partial with a continuation reason. This is a status check, not a reading-depth measurement.',
                                   category='evidence' if field == 'freshness' else 'validation',
                                   current=present(row, field), expected=target, table='entities', row=row, source_paths=paths(row))
        if task['kind'] in ('analyze', 'update'):
            for id in task['source_ids']:
                row = sources.get(id)
                if row is None:
                    continue  # The graph/reference gate reports a missing source.
                for field, target in [('read_state', 'read'), ('symbols_complete', True)]:
                    if field == 'symbols_complete' and row.get('category') != 'source':
                        continue
                    if row.get(field) != target:
                        issues.add('source_' + field, field, 'task completion requires this source review status',
                                   'Set this field only after the complete file and declarations have actually been reviewed. Reuse delivered lines on this connection; read only missing ranges. Otherwise retain partial and describe remaining work.',
                                   current=present(row, field), expected=target, table='sources', row=row, source_paths=[row.get('path', id)])
        for table, ids in task.get('review_ids', {}).items():
            rows = {r['id']: r for r in graph.get(table, []) if isinstance(r, dict) and isinstance(r.get('id'), str)}
            for id in ids:
                row = rows.get(id)
                if row is not None and row.get('freshness', 'current') != 'current':
                    issues.add('update_record_stale', 'freshness', 'update task still has stale records to reconcile',
                               'Reconcile this record with current source evidence, or retire it only through the update protocol when justified.',
                               category='evidence', current=present(row, 'freshness'), expected='current', table=table, row=row)
    return issues


def preparation_contract():
    return {'function_details_template': {field: None for field in DETAIL_FIELDS},
            'template_note': 'Null is an UNFILLED placeholder, not a valid reviewed finding. Replace every field with an array from actual findings; [] means checked and absent. Never auto-fill semantic arrays.',
            'done_requirements': {'analyze_update_scope': 'Every task.scope_ids node, including file/module nodes: analysis=reviewed and freshness=current.',
                                  'analyze_update_sources': 'Every task.source_ids file: read_state=read; source-category files also need symbols_complete=true.',
                                  'reviewed_functions': 'Nonempty summary, supporting evidence_ids, and all six details arrays.',
                                  'other_checks': 'Graph references, current source evidence, snapshot, revision and lease still apply.'},
            'on_error': 'Read issues[].field/current/expected/action and pending_checks. Fix independent issues together. Keep the same connection/read ledger and reuse findings; do not blindly repeat a rejected payload.',
            'guide': 'blueprint_guide(topic="preparation") contains full submission examples.'}
