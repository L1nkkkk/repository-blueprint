"""Explain update scope using verified snapshots; uncertain changes stay conservative."""
import ast
from collections import Counter
from difflib import SequenceMatcher

from .repository import source_bytes, decode_text


def snapshot_texts(graph):
    texts = {}
    root = graph['project'].get('source_root')
    if not root:
        return texts
    for source in graph['sources']:
        if not source['included'] or not source.get('encoding'):
            continue
        try:
            _, raw = source_bytes(graph, root, source['id'])
            texts[source['sha256']] = decode_text(raw)[0]
        except (OSError, ValueError):
            # Absence is explicit; never substitute newer bytes for an old hash.
            pass
    return texts


def exact_renames(before, after):
    old_counts = Counter(s['sha256'] for s in before.values())
    new_counts = Counter(s['sha256'] for s in after.values())
    new_by_hash = {s['sha256']: p for p, s in after.items() if p not in before and s.get('bytes', 0)}
    result = []
    for path in sorted(before.keys() - after.keys()):
        source = before[path]
        target = new_by_hash.get(source['sha256'])
        if target and old_counts[source['sha256']] == new_counts[source['sha256']] == 1:
            other = after[target]
            if source['language'] == other['language'] and source['category'] == other['category']:
                result.append({'from': path, 'to': target, 'basis': 'identical_content'})
    return result


def cosmetic_signature(text, language):
    if language == 'python':
        try:
            return ast.dump(ast.parse(text, type_comments=True), include_attributes=False)
        except (SyntaxError, ValueError):
            return None
    if language not in {'cpp', 'csharp', 'javascript', 'typescript', 'java'}:
        return None
    # Conservative lexical comparison. Preserve line breaks (ASI / preprocessor),
    # strings and operators; opt out of forms needing a full language parser.
    if any(value in text for value in ('`', 'R"', '@"', '"""', '#', '__LINE__')):
        return None
    if language in {'javascript', 'typescript'} and any(value in text for value in ('@ts-', '@jsx', '@type', '@param', '@return', '<reference', 'sourceMappingURL', 'sourceURL')):
        return None
    tokens, index = [], 0
    while index < len(text):
        c = text[index]
        if text.startswith('//', index):
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
        elif text.startswith('/*', index):
            end = text.find('*/', index + 2)
            if end < 0:
                return None
            tokens.extend('\n' for _ in range(text[index:end + 2].count('\n')))
            index = end + 2
        elif c in "\"'":
            end = index + 1
            while end < len(text):
                if text[end] == '\\':
                    end += 2
                elif text[end] == c:
                    end += 1
                    break
                else:
                    end += 1
            else:
                return None
            tokens.append(text[index:end]); index = end
        elif c == '/':
            return None  # Could be a regular-expression literal.
        elif c == '\n':
            tokens.append(c); index += 1
        elif c.isspace():
            index += 1
        elif c.isalnum() or c in '_$':
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] in '_$'):
                end += 1
            tokens.append(text[index:end]); index = end
        else:
            # Keep adjacent punctuation distinct from whitespace-separated tokens.
            end = index + 1
            while end < len(text) and text[end] in '+-*=&|<>!?.:%^~':
                end += 1
            tokens.append(text[index:end]); index = end
    return tokens


def classify_change(graph, source, old, new):
    result = {'path': source['path'], 'kind': 'unknown', 'reason': '缺少可比较的历史源码，按已记录依赖保守复核。'}
    if old is None or new is None:
        return result
    language = source['language']
    a, b = cosmetic_signature(old, language), cosmetic_signature(new, language)
    if a is not None and a == b:
        return {**result, 'kind': 'cosmetic', 'reason': '语法内容保持一致；复核本文件、依据位置及直接读取文件内容的依赖。'}
    if source['category'] == 'build':
        return {**result, 'kind': 'build', 'reason': '构建或配置变化可能影响解析条件，扩大复核范围。'}
    if language != 'python':
        return {**result, 'kind': 'content', 'reason': '内容发生变化；按已记录调用与数据依赖复核。'}
    try:
        old_tree, new_tree = ast.parse(old, type_comments=True), ast.parse(new, type_comments=True)
        def contract(tree):
            # Module statements, signatures, decorators and defaults form the contract.
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node.body = [ast.Pass()]
            return ast.dump(tree, include_attributes=False)
        if contract(ast.parse(old, type_comments=True)) != contract(new_tree):
            return {**result, 'kind': 'contract', 'reason': '声明、导入、默认参数或模块状态变化，复核使用方。'}
        changed_lines = set()
        for tag, start, end, _, _ in SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
            if tag != 'equal':
                changed_lines.update(range(max(1, start + 1), max(start + 2, end + 1)))
        functions = [n for n in ast.walk(old_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        enclosing = [n for n in functions if changed_lines & set(range(n.lineno, n.end_lineno + 1))]
        covered = set().union(*(set(range(n.lineno, n.end_lineno + 1)) for n in enclosing)) if enclosing else set()
        if not changed_lines <= covered:
            return result
        proofs = {e['id']: e for e in graph['evidence'] if e['source_id'] == source['id']}
        seeds = [e['id'] for e in graph['entities'] if e['kind'] in {'function', 'method'} and any(
            p in proofs and changed_lines & set(range(proofs[p]['start_line'], proofs[p]['end_line'] + 1)) for p in e['evidence_ids'])]
        if seeds:
            return {**result, 'kind': 'implementation', 'entity_ids': seeds, 'reason': '函数实现变化且声明保持一致，从相关函数沿已记录依赖复核。'}
    except (SyntaxError, ValueError, RecursionError):
        pass
    return result
