"""Bounded, non-executing syntax extraction. Syntax sites are not resolved flows."""

import ast
from collections import Counter
import importlib
from importlib.metadata import version, PackageNotFoundError
import sys

from .core import digest

VERSION = 'structure-1'
BACKENDS = {'cpp': ('tree_sitter_cpp', 'language'), 'csharp': ('tree_sitter_c_sharp', 'language'),
            'javascript': ('tree_sitter_javascript', 'language'),
            'typescript': ('tree_sitter_typescript', 'language_typescript'),
            'tsx': ('tree_sitter_typescript', 'language_tsx')}
MAX_SYMBOLS = 20000
MAX_VISITS = 250000
MAX_SITES = 20000
TYPES = {'class', 'struct', 'interface', 'enum'}


def backend(language, path=''):
    if language == 'python':
        return {'id': f'{VERSION}:python-ast:{sys.version_info.major}.{sys.version_info.minor}', 'available': True}
    key = 'tsx' if language == 'typescript' and path.lower().endswith('.tsx') else language
    if key not in BACKENDS:
        return {'id': VERSION + ':unsupported:' + language, 'available': False, 'reason': 'No structural backend for this language.'}
    module, _ = BACKENDS[key]
    try:
        importlib.import_module('tree_sitter')
        importlib.import_module(module)
        versions = [version('tree-sitter'), version(module.replace('_', '-'))]
        return {'id': VERSION + ':' + key + ':' + ':'.join(versions), 'available': True, 'key': key}
    except (ImportError, OSError, PackageNotFoundError) as error:
        return {'id': VERSION + ':missing:' + key, 'available': False, 'key': key,
                'reason': 'Install requirements-parsers.txt with the Python used by this tool. ' + str(error)}


def capabilities():
    return {name: backend(name) for name in ('python', 'cpp', 'csharp', 'typescript', 'javascript')}


class Output:
    def __init__(self, source_id):
        self.source_id = source_id
        self.symbols, self.sites, self.diagnostics = [], [], []
        self.counts = Counter()
        self.ordinals = Counter()

    def issue(self, message, line=None):
        self.counts['diagnostics'] += 1
        if len(self.diagnostics) < 50:
            self.diagnostics.append({'message': message, 'line': line})

    def symbol(self, kind, name, parent, start, end, signature='', parameters=(), declaration_kind=''):
        if len(self.symbols) >= MAX_SYMBOLS:
            raise ValueError('Symbol limit reached; remaining declarations require review.')
        name = name.strip() or '<anonymous>'
        qualified = (parent['qualified_name'] + '.' if parent else '') + name
        # Bodies and line numbers deliberately do not participate in identity.
        identity = [self.source_id, kind, qualified, ' '.join(signature.split())]
        key = digest(identity)
        ordinal = self.ordinals[key]
        self.ordinals[key] += 1
        row = {'id': 'symbol:' + digest([*identity, ordinal])[:24], 'kind': kind, 'name': name,
               'qualified_name': qualified, 'parent_id': parent['id'] if parent else None,
               'start_line': start, 'end_line': end, 'signature': signature[:1200],
               'signature_truncated': len(signature) > 1200, 'parameters': list(parameters)[:100],
               'parameter_count': len(parameters), 'declaration_kind': declaration_kind}
        self.symbols.append(row)
        return row

    def site(self, kind, text, parent, start, end):
        if len(self.sites) >= MAX_SITES:
            if not self.counts['sites_limited']:
                self.issue('Syntax site limit reached; remaining references require review.')
            self.counts['sites_limited'] += 1
            return
        self.sites.append({'kind': kind, 'text': text[:400], 'text_truncated': len(text) > 400,
                           'enclosing_symbol_id': parent['id'] if parent else None,
                           'start_line': start, 'end_line': end, 'resolution': 'unresolved'})


def python_parse(text, output):
    tree = ast.parse(text, type_comments=True)
    pending = [(tree, None)]
    visits = 0
    while pending:
        node, parent = pending.pop()
        visits += 1
        if visits > MAX_VISITS:
            raise ValueError('Syntax node limit reached; remaining declarations require review.')
        new_parent = parent
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            is_class = isinstance(node, ast.ClassDef)
            kind = 'class' if is_class else 'method' if parent and parent['kind'] in TYPES else 'function'
            params = []
            if not is_class:
                for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                          *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
                    params.append({'name': a.arg, 'type': ast.unparse(a.annotation) if a.annotation else '',
                                   'start_line': a.lineno, 'end_line': a.end_lineno})
            signature = ('class ' + node.name + '(' + ', '.join(ast.unparse(b) for b in node.bases) + ')'
                         if is_class else ('async ' if isinstance(node, ast.AsyncFunctionDef) else '') +
                         'def ' + node.name + '(' + ast.unparse(node.args) + ')' +
                         (' -> ' + ast.unparse(node.returns) if node.returns else ''))
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            new_parent = output.symbol(kind, node.name, parent, start, node.end_lineno, signature, params, type(node).__name__)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and (parent is None or parent['kind'] in TYPES):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    annotation = getattr(node, 'annotation', None)
                    signature = target.id + (': ' + ast.unparse(annotation) if annotation else '')
                    output.symbol('field' if parent else 'variable', target.id, parent,
                                  node.lineno, node.end_lineno, signature, declaration_kind=type(node).__name__)
        elif isinstance(node, ast.Call):
            output.site('call', ast.unparse(node.func), parent, node.lineno, node.end_lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            output.site('import', ast.unparse(node), parent, node.lineno, node.end_lineno)
        pending.extend((child, new_parent) for child in reversed(list(ast.iter_child_nodes(node))))


def tree_parse(text, key, output):
    from tree_sitter import Language, Parser
    module, function = BACKENDS[key]
    parser = Parser(Language(getattr(importlib.import_module(module), function)()))
    raw = text.encode('utf-8')
    tree = parser.parse(raw)

    def content(node):
        return raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''

    def field(node, name):
        return node.child_by_field_name(name) if node else None

    def line_end(node):
        return max(node.start_point.row + 1, node.end_point.row + (1 if node.end_point.column else 0))

    def declarator_name(node):
        while node and node.type not in {'identifier', 'field_identifier', 'qualified_identifier',
                                          'operator_name', 'destructor_name', 'structured_binding_declarator'}:
            child = field(node, 'declarator')
            if child is None and node.type in {'reference_declarator', 'pointer_declarator', 'parenthesized_declarator'}:
                child = next((c for c in node.named_children if 'declarator' in c.type or c.type in {'identifier', 'field_identifier'}), None)
            node = child
        return content(node)

    scope_kinds = {'namespace_definition': 'namespace', 'namespace_declaration': 'namespace',
                   'file_scoped_namespace_declaration': 'namespace', 'internal_module': 'namespace',
                   'class_specifier': 'class', 'struct_specifier': 'struct', 'union_specifier': 'struct',
                   'enum_specifier': 'enum', 'class_declaration': 'class', 'abstract_class_declaration': 'class',
                   'struct_declaration': 'struct', 'record_declaration': 'class', 'interface_declaration': 'interface',
                   'enum_declaration': 'enum'}
    callable_types = {'function_definition', 'function_declaration', 'generator_function_declaration',
                      'method_definition', 'method_declaration', 'constructor_declaration', 'destructor_declaration',
                      'operator_declaration', 'conversion_operator_declaration', 'local_function_statement',
                      'method_signature', 'abstract_method_signature', 'function_signature', 'arrow_function',
                      'function_expression', 'generator_function'}
    field_types = {'property_declaration', 'property_signature', 'field_definition', 'public_field_definition',
                   'enum_member_declaration', 'enumerator'}
    pending = [(tree.root_node, None)]
    file_namespace = None
    visits = 0
    while pending:
        node, parent = pending.pop()
        if node.parent == tree.root_node and file_namespace and node.type != 'file_scoped_namespace_declaration':
            parent = file_namespace
        visits += 1
        if visits > MAX_VISITS:
            raise ValueError('Syntax node limit reached; remaining declarations require review.')
        if node.is_error or node.is_missing:
            output.issue('Syntax recovery at ' + node.type + '; this region requires manual review.', node.start_point.row + 1)
        kind, name, params_node, declaration = None, '', None, node
        body = field(node, 'body')
        if node.type in scope_kinds:
            kind = scope_kinds[node.type]
            name = content(field(node, 'name')) or '<anonymous ' + kind + '>'
        elif node.type in callable_types:
            kind = 'method' if 'method' in node.type or node.type in {'constructor_declaration', 'destructor_declaration', 'operator_declaration', 'conversion_operator_declaration'} or parent and parent['kind'] in TYPES else 'function'
            if key == 'cpp':
                decl = field(node, 'declarator')
                while decl and decl.type != 'function_declarator':
                    decl = field(decl, 'declarator')
                name = declarator_name(field(decl, 'declarator'))
                params_node = field(decl, 'parameters')
                if '::' in name:
                    scope, short_name = name.rsplit('::', 1)
                    matches = [s for s in output.symbols if s['qualified_name'] == scope.replace('::', '.') and s['kind'] in TYPES | {'namespace'}]
                    if len(matches) == 1:
                        parent, name = matches[0], short_name
                        kind = 'method' if parent['kind'] in TYPES else 'function'
            else:
                name = content(field(node, 'name'))
                if not name and node.parent:
                    p = node.parent
                    if p.type in {'variable_declarator', 'pair', 'assignment_expression', 'public_field_definition', 'field_definition'}:
                        name = content(field(p, 'name') or field(p, 'key') or field(p, 'left') or field(p, 'property'))
                name = name or '<anonymous function>'
                params_node = field(node, 'parameters') or field(node, 'parameter')
        elif key == 'cpp' and node.type in {'declaration', 'field_declaration'}:
            # Function-pointer variables are not function declarations.
            decl = field(node, 'declarator')
            while decl and decl.type in {'pointer_declarator', 'reference_declarator'}:
                decl = field(decl, 'declarator')
            if decl and decl.type == 'function_declarator' and field(decl, 'declarator') and field(decl, 'declarator').type != 'parenthesized_declarator':
                kind = 'method' if parent and parent['kind'] in TYPES else 'function'
                name = declarator_name(field(decl, 'declarator'))
                params_node = field(decl, 'parameters')
        elif node.type in field_types:
            kind, name = 'field', content(field(node, 'name') or field(node, 'property'))
        elif node.type == 'variable_declarator' and (parent is None or parent['kind'] in TYPES | {'namespace'}):
            value = field(node, 'value')
            if value is None or value.type not in callable_types:
                kind = 'field' if parent and parent['kind'] in TYPES else 'variable'
                name = content(field(node, 'name'))
        new_parent = parent
        if kind and name:
            parameters = []
            params_node = params_node or field(node, 'parameters') or next((c for c in node.named_children if c.type == 'parameter_list'), None)
            if params_node:
                children = params_node.named_children if params_node.type in {'parameter_list', 'formal_parameters'} else [params_node]
                for p in children:
                    if p.type == 'comment':
                        continue
                    pname = content(field(p, 'name') or field(p, 'pattern') or field(p, 'left'))
                    if not pname:
                        pname = declarator_name(field(p, 'declarator')) if key == 'cpp' else content(p)
                    parameters.append({'name': pname[:240], 'type': content(field(p, 'type'))[:240],
                                       'text': content(p)[:400], 'start_line': p.start_point.row + 1, 'end_line': line_end(p)})
            cutoff = body.start_byte if body else (field(node, 'value') or field(node, 'accessors') or node).start_byte
            if cutoff <= node.start_byte:
                cutoff = node.end_byte
            if key == 'cpp' and node.parent and node.parent.type == 'template_declaration':
                declaration = node.parent
            signature = raw[declaration.start_byte:cutoff].decode('utf-8').strip().rstrip(';')
            if kind in TYPES | {'namespace'}:
                signature = ' '.join(signature.split())
            new_parent = output.symbol(kind, name, parent, declaration.start_point.row + 1, line_end(node),
                                       signature, parameters, declaration.type)
            if node.type == 'file_scoped_namespace_declaration':
                new_parent['end_line'] = max(1, len(text.splitlines()))
                file_namespace = new_parent
        if key == 'cpp' and not kind and node.type in {'declaration', 'field_declaration'} and (parent is None or parent['kind'] in TYPES | {'namespace'}):
            for decl in node.children_by_field_name('declarator'):
                dname = declarator_name(decl)
                if not dname and decl.type == 'init_declarator':
                    dname = declarator_name(field(decl, 'declarator'))
                if dname:
                    output.symbol('field' if parent and parent['kind'] in TYPES else 'variable', dname, parent,
                                  node.start_point.row + 1, line_end(node), content(field(node, 'type')) + ' ' + dname,
                                  declaration_kind=node.type)
        if node.type in {'call_expression', 'invocation_expression', 'object_creation_expression', 'new_expression'}:
            target = field(node, 'function') or field(node, 'constructor') or field(node, 'type')
            output.site('call', content(target) or content(node), parent, node.start_point.row + 1, line_end(node))
        elif node.type in {'import_statement', 'import_declaration', 'using_directive', 'preproc_include'}:
            output.site('import', content(node), parent, node.start_point.row + 1, line_end(node))
        pending.extend((child, new_parent) for child in reversed(node.named_children))


def parse(source, text):
    engine = backend(source['language'], source['path'])
    output = Output(source['id'])
    state = 'parsed'
    if not engine['available']:
        state = 'unavailable' if 'key' in engine else 'unsupported'
        output.issue(engine['reason'])
    else:
        try:
            if source['language'] == 'python':
                python_parse(text, output)
            else:
                tree_parse(text, engine['key'], output)
        except (SyntaxError, ValueError, RecursionError, OverflowError) as error:
            output.issue(str(error), getattr(error, 'lineno', None))
        if output.counts['diagnostics']:
            state = 'partial'
    return {'source_id': source['id'], 'path': source['path'], 'sha256': source['sha256'],
            'language': source['language'], 'parser_id': engine['id'], 'state': state,
            'symbols': output.symbols, 'sites': output.sites, 'diagnostics': output.diagnostics,
            'diagnostic_count': output.counts['diagnostics'],
            'limitations': 'Syntax only. No macro expansion, build/type resolution, dynamic dispatch resolution or runtime/data-flow proof. Anonymous scopes and duplicate declarations retain separate identities.'}
