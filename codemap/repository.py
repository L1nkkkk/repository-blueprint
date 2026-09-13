"""Whole-tree inventory and bounded, snapshot-checked reading. No AI inference."""

from __future__ import annotations

from fnmatch import fnmatchcase
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat

from .core import TABLES, digest, require, validate_graph


LANGUAGES = {
    **dict.fromkeys(('.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx', '.inl'), 'cpp'),
    **dict.fromkeys(('.py', '.pyi'), 'python'), '.cs': 'csharp',
    **dict.fromkeys(('.ts', '.tsx', '.mts', '.cts'), 'typescript'),
    **dict.fromkeys(('.js', '.jsx', '.mjs', '.cjs'), 'javascript'),
    '.rs': 'rust', '.go': 'go', '.java': 'java', '.lua': 'lua', '.sh': 'shell', '.ps1': 'powershell',
}
METADATA_DIRS = {'.git', '.hg', '.svn', '.codemap'}
BUILD_NAMES = {'cmakelists.txt', 'makefile', 'dockerfile', 'package.json', 'pyproject.toml', 'cargo.toml', 'go.mod'}
BUILD_SUFFIXES = {'.cmake', '.sln', '.csproj', '.vcxproj', '.props', '.targets', '.uproject', '.uplugin', '.json', '.toml', '.yaml', '.yml', '.ini', '.cfg'}
MAX_TEXT_BYTES = 2 * 1024 * 1024


def identity(kind, path):
    return f'{kind}:{sha256(path.encode("utf-8")).hexdigest()[:24]}'


def classify(path):
    p = PurePosixPath(path)
    suffix, name = p.suffix.lower(), p.name.lower()
    language = LANGUAGES.get(suffix, '')
    category = 'source' if language else 'resource'
    if name in BUILD_NAMES or suffix in BUILD_SUFFIXES or name.endswith(('.build.cs', '.target.cs')):
        category = 'build'
    elif suffix in {'.md', '.rst', '.txt', '.adoc'} or name.startswith(('readme', 'license', 'copying')):
        category = 'documentation'
    parts = {part.lower() for part in p.parts[:-1]}
    roles = []
    if parts & {'tests', 'test', '__tests__', 'spec', 'specs'} or name.startswith('test_') or '.test.' in name or '.spec.' in name:
        roles.append('test')
    if parts & {'tools', 'scripts', 'utilities'}:
        roles.append('tool')
    if parts & {'vendor', 'thirdparty', 'third_party', 'node_modules', '.venv', 'venv'}:
        roles.append('vendor')
    if parts & {'__pycache__', 'intermediate', 'binaries', 'dist', 'generated'} or name.endswith(('.g.cs', '.generated.h')):
        roles.append('generated')
    return category, language, roles


def decode_text(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16'), 'utf-16'
    if b'\x00' in raw:
        raise ValueError('binary content')
    return raw.decode('utf-8-sig'), 'utf-8'


def fingerprint_file(path):
    """Stream hashes; retain bounded text only for encoding classification."""
    before = path.stat()
    hasher, sample, size = sha256(), bytearray(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode), 'not a regular file')
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
            if len(sample) <= MAX_TEXT_BYTES:
                sample.extend(chunk[:MAX_TEXT_BYTES + 1 - len(sample)])
        after = os.fstat(stream.fileno())
    require((before.st_mtime_ns, before.st_size, before.st_ino) == (after.st_mtime_ns, after.st_size, after.st_ino) and size == after.st_size, 'file changed during scan')
    encoding, reason = '', ''
    if size > MAX_TEXT_BYTES:
        reason = 'File exceeds the 2 MiB text-reading limit; retained as required work.'
    else:
        try:
            _, encoding = decode_text(bytes(sample))
        except (UnicodeError, ValueError):
            reason = 'Binary or unsupported text encoding; retained in inventory.'
    return hasher.hexdigest(), size, encoding, reason


def entity(kind, path, name=None, sources=None, language='', roles=None):
    return {'id': identity(kind, path), 'kind': kind, 'name': name or PurePosixPath(path).name,
            'language': language, 'roles': roles or [], 'source_ids': sources or [],
            'analysis': 'located', 'freshness': 'current', 'summary': '', 'evidence_ids': []}


def task(id, kind, scopes, sources, dependencies=None, reason='', blocked=False):
    return {'id': id, 'kind': kind, 'scope_ids': scopes, 'source_ids': sources,
            'depends_on': dependencies or [], 'required': True, 'state': 'blocked' if blocked else 'queued',
            'attempt': 0, 'lease': None, 'reason': reason}


def scan_repository(root, *, excludes=()):
    """Enumerate all files, including gitignored/independent/test/vendor code.

    Links and directory read errors remain visible gaps. Only explicitly named
    exclusions and VCS/map metadata are pruned. Scanning never means AI reading.
    """
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), 'repository root must be a directory')
    require(all(isinstance(p, str) and p and '\\' not in p and not p.startswith('/') and '..' not in PurePosixPath(p).parts for p in excludes), 'exclusions must be repository-relative patterns')
    project_id = identity('project', os.path.normcase(str(root)))
    graph = {'format_version': '0.1', **{name: [] for name in TABLES}, 'receipts': {}}
    inventory = {'policy': 'whole-tree-v1', 'exclusion_patterns': list(excludes), 'excluded_paths': [], 'gaps': [], 'empty_directories': []}
    graph['inventory'] = inventory
    repo = entity('repository', '.', root.name)
    graph['entities'].append(repo)
    directories = {'.': repo}

    def directory(rel):
        if rel in directories:
            return directories[rel]
        parent = directory(str(PurePosixPath(rel).parent))
        row = entity('directory', rel)
        directories[rel] = row
        graph['entities'].append(row)
        graph['memberships'].append({'id': identity('member', rel), 'axis': 'physical', 'parent_id': parent['id'], 'child_id': row['id']})
        return row

    pending = [root]
    directory_stamps = []
    while pending:
        folder = pending.pop()
        rel_folder = folder.relative_to(root).as_posix()
        try:
            before = folder.stat()
            with os.scandir(folder) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
            directory_stamps.append((folder, before.st_mtime_ns))
        except OSError as error:
            inventory['gaps'].append({'path': rel_folder, 'reason': str(error)})
            continue
        if not entries:
            inventory['empty_directories'].append(rel_folder)
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            exclusion = next((p for p in excludes if fnmatchcase(rel, p)), None)
            if entry.name in METADATA_DIRS or exclusion:
                inventory['excluded_paths'].append({'path': rel, 'reason': f'Explicit pattern: {exclusion}' if exclusion else 'VCS metadata or code-map output directory'})
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                reparse = getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
                if entry.is_symlink() or reparse:
                    inventory['gaps'].append({'path': rel, 'reason': 'Symbolic link or reparse point was not followed; its target is outside this inventory.'})
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                require(stat.S_ISREG(info.st_mode), 'not a regular file')
                hash_value, size, encoding, reason = fingerprint_file(path)
                category, language, roles = classify(rel)
                # Non-text resources are catalogued but not sent for code reading.
                included = bool(encoding) or category in {'source', 'build', 'documentation'} or size > MAX_TEXT_BYTES
                source = {'id': identity('source', rel), 'path': rel, 'category': category, 'language': language,
                          'sha256': hash_value, 'bytes': size, 'encoding': encoding,
                          'included': included, 'read_state': 'failed' if included and not encoding else 'unread',
                          'symbols_complete': False, 'reason': reason}
                graph['sources'].append(source)
                file = entity('file', rel, sources=[source['id']], language=language, roles=roles)
                graph['entities'].append(file)
                parent = directory(str(PurePosixPath(rel).parent))
                graph['memberships'].append({'id': identity('member', 'file/' + rel), 'axis': 'physical', 'parent_id': parent['id'], 'child_id': file['id']})
                if included:
                    graph['tasks'].append(task(identity('task', rel), 'analyze', [file['id']], [source['id']], reason=reason, blocked=not encoding))
            except (OSError, ValueError) as error:
                inventory['gaps'].append({'path': rel, 'reason': str(error)})
    for folder, stamp in directory_stamps:
        try:
            if folder.stat().st_mtime_ns != stamp:
                inventory['gaps'].append({'path': folder.relative_to(root).as_posix(), 'reason': 'Directory contents changed during scan; repeat inventory from a stable tree.'})
        except OSError as error:
            inventory['gaps'].append({'path': folder.relative_to(root).as_posix(), 'reason': str(error)})
    for table in ('sources', 'entities', 'memberships', 'tasks'):
        graph[table].sort(key=lambda row: row.get('path', row['id']))
    for key in ('gaps', 'excluded_paths'):
        inventory[key].sort(key=lambda row: row['path'])
    inventory['empty_directories'].sort()
    if inventory['gaps']:
        graph['tasks'].append(task('task:inventory-gaps', 'inventory', [], [], reason='Resolve inventory gaps and create a fresh project scan.', blocked=True))
    required_files = [s['id'] for s in graph['sources'] if s['included']]
    graph['tasks'].append(task('task:repository-review', 'link', [d['id'] for d in directories.values()], required_files,
                               [t['id'] for t in graph['tasks']], reason='Review repository responsibilities, directory summaries and cross-file relations after all files.'))
    graph['project'] = {'id': project_id, 'name': root.name, 'snapshot_id': 'snapshot:' + digest({'sources': [(s['path'], s['sha256'], s['included']) for s in graph['sources']], 'inventory': inventory}),
                        'revision': 0, 'mode': 'whole_repository', 'inventory_complete': not inventory['gaps'],
                        'execution': 'active', 'source_root': str(root)}
    return validate_graph(graph)


def source_record(graph, reference):
    result = next((s for s in graph['sources'] if reference in (s['id'], s['path'])), None)
    require(result is not None, 'source is not in this project inventory')
    return result


def source_bytes(graph, root, reference):
    source = source_record(graph, reference)
    root = Path(root).resolve(strict=True)
    candidate = root / source['path']
    # Reject links introduced after scanning, including an in-root alias.
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        info = part.lstat()
        require(not part.is_symlink() and not (getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)), 'source path now contains a link')
    require(candidate.resolve(strict=True).is_relative_to(root), 'source resolves outside repository')
    require(candidate.is_file(), 'source is unavailable')
    require(candidate.stat().st_size <= MAX_TEXT_BYTES, 'file exceeds the 2 MiB reading limit')
    with candidate.open('rb') as stream:
        content = stream.read(MAX_TEXT_BYTES + 1)
    require(len(content) <= MAX_TEXT_BYTES, 'file exceeds the 2 MiB reading limit')
    require(sha256(content).hexdigest() == source['sha256'], 'source snapshot changed; use blueprint_update to preview/apply an update before reading this version')
    return source, content


def read_source(graph, root, reference, *, start=1, limit=160, with_context=False):
    require(type(start) is int and start >= 1 and type(limit) is int and 1 <= limit <= 500, 'read range must start at 1 or later and contain at most 500 lines')
    source, raw = source_bytes(graph, root, reference)
    try:
        content, _ = decode_text(raw)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'Cannot display source text: {error}') from error
    lines = content.splitlines() or ['']
    require(start <= max(1, len(lines)), 'start line exceeds file')
    end = min(start - 1 + limit, len(lines))
    result = {'source_id': source['id'], 'path': source['path'], 'language': source['language'], 'sha256': source['sha256'], 'start_line': start,
            'end_line': end, 'total_lines': len(lines), 'empty_file': not content, 'next_start': end + 1 if end < len(lines) else None,
            'lines': [{'number': n + 1, 'text': lines[n]} for n in range(start - 1, end)]}
    # Highlighting an excerpt needs preceding text to preserve multiline token state.
    # This is still the same hash-checked file under the existing 2 MiB reading limit.
    if with_context:
        result['highlight_prefix'] = ''.join(line + '\n' for line in lines[:start - 1])
    return result


def search_sources(graph, root, query, *, limit=100, paths='*'):
    require(isinstance(query, str) and query and len(query) <= 1000, 'search requires 1–1000 characters')
    require(type(limit) is int and 1 <= limit <= 500, 'search limit must be between 1 and 500')
    matches, failures, truncated = [], [], False
    for source in graph['sources']:
        if not source['included'] or not fnmatchcase(source['path'], paths):
            continue
        try:
            _, raw = source_bytes(graph, root, source['id'])
            content, _ = decode_text(raw)
            for line, text in enumerate(content.splitlines(), 1):
                if query.casefold() in text.casefold():
                    if len(matches) < limit:
                        matches.append({'source_id': source['id'], 'path': source['path'], 'line': line, 'text': text[:2000]})
                    else:
                        truncated = True
        except (OSError, ValueError) as error:
            failures.append({'path': source['path'], 'reason': str(error)})
    return {'matches': matches, 'truncated': truncated, 'failures': failures}
