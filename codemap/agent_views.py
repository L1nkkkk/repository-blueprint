"""Bounded MCP views. Summaries never replace the stored analysis records."""

from .core import require
from .repository import source_record, source_bytes


def bounded(value, *, limit=5):
    if isinstance(value, dict):
        return {k: bounded(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return {'count': len(value), 'sample': [bounded(v, limit=limit) for v in value[:limit]],
                'truncated': len(value) > limit}
    if isinstance(value, str) and len(value) > 360:
        return value[:360] + '… [summary; request full detail]'
    return value


def status_view(report, detail='summary'):
    if detail == 'full':
        return report
    result = {key: bounded(value) for key, value in report.items() if key != 'last_update'}
    result['project'] = report['project']
    update = report.get('last_update')
    result['last_update'] = ({key: update[key] for key in ('plan_id', 'base_revision', 'revision', 'snapshot_id') if key in update}
                             if update else None)
    result['detail'] = 'summary'
    result['full_detail'] = 'blueprint_status(detail="full"); blueprint_query for individual records'
    return result


def entity_summary(row):
    fields = ('id', 'kind', 'name', 'language', 'roles', 'source_ids', 'analysis', 'freshness', 'summary')
    result = {key: row[key] for key in fields if key in row}
    result['summary'] = bounded(result.get('summary', ''))
    result['evidence_count'] = len(row.get('evidence_ids', []))
    return result


def claim_view(claim, detail='summary'):
    if detail == 'full':
        return claim
    if claim['state'] != 'claimed':
        return {**claim, 'status': status_view(claim['status'])}
    result = {key: claim[key] for key in ('state', 'project', 'batch_template')}
    result['task'] = bounded(claim['task'], limit=20)
    # Lease identity, deadlines and worker identity must remain exact.
    result['task']['lease'] = claim['task']['lease']
    result['sources'] = claim['sources'][:20]
    result['source_count'] = len(claim['sources'])
    result['entities'] = [entity_summary(row) for row in claim['entities'][:20]]
    result['entity_count'] = len(claim['entities'])
    result['review_ids'] = bounded(claim['review_ids'], limit=10)
    if 'structure' in claim:
        result['structure'] = claim['structure']
    result['detail'] = 'summary'
    result['full_records'] = 'Use blueprint_query for the task and its source/entity/review IDs; summaries are not full upsert records. Use blueprint_prepare to merge changes.'
    return result


def saved_context(graph, *, source=None, entity_id=None, offset=0, limit=20, revision=None):
    """Check only the selected page's recorded source dependencies, once per file."""
    require(source is not None or entity_id is not None, 'context requires source or entity_id')
    current_revision = graph['project']['revision']
    require(revision in (None, current_revision), 'Graph revision changed; restart context pagination.')
    selected_source = source_record(graph, source) if source is not None else None
    rows = graph['entities']
    if entity_id is not None:
        require(any(row['id'] == entity_id for row in rows), 'unknown entity')
        rows = [row for row in rows if row['id'] == entity_id]
    if selected_source:
        rows = [row for row in rows if selected_source['id'] in row['source_ids']]
    proofs = {row['id']: row for row in graph['evidence']}
    checks = {}

    def check(source_id):
        if source_id not in checks:
            try:
                source_bytes(graph, graph['project']['source_root'], source_id)
                checks[source_id] = 'current'
            except (OSError, ValueError) as error:
                checks[source_id] = str(error)
        return checks[source_id] == 'current'

    records = []
    for row in rows[offset:offset + limit]:
        evidence = [proofs[id] for id in row.get('evidence_ids', [])]
        source_ids = set(row['source_ids']) | {p['source_id'] for p in evidence}
        # Evaluate every supporting source even when an earlier check failed.
        current_sources = [check(id) for id in sorted(source_ids)]
        reusable = (row['analysis'] == 'reviewed' and row.get('freshness', 'current') == 'current'
                    and bool(evidence) and all(p.get('freshness', 'current') == 'current' for p in evidence)
                    and all(current_sources))
        records.append({**entity_summary(row), 'reuse': 'current_evidence' if reusable else 'needs_review'})
    return {'revision': current_revision, 'source_id': selected_source['id'] if selected_source else None,
            'records': records, 'total': len(rows), 'next_offset': offset + limit if offset + limit < len(rows) else None,
            'source_checks': checks, 'detail': 'summary',
            'reuse_scope': 'Saved claims and their recorded evidence only. Query full details by ID before reuse. New call sites, changed interfaces and unrecorded dependencies require targeted reading. No reading or task progress is recorded by this lookup.'}
