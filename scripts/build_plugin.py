"""Build a self-contained local plugin or zip, without touching analysis projects."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

NAME = 'repository-blueprint'
ROOT = Path(__file__).resolve().parent.parent


def payload_files(source=ROOT, *, kind='codex'):
    packaged = (source / 'runtime/codemap').is_dir()
    code = source / ('runtime/codemap' if packaged else 'codemap')
    manifest = source / ('.codex-plugin/plugin.json' if packaged else f'packaging/{NAME}/.codex-plugin/plugin.json')
    files = {'.codex-plugin/plugin.json': manifest.read_bytes()} if kind == 'codex' else {}
    for path in sorted(code.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            files['runtime/codemap/' + path.relative_to(code).as_posix()] = path.read_bytes()
    skill = source / 'skills' / NAME
    for path in sorted(skill.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            files[f'skills/{NAME}/' + path.relative_to(skill).as_posix()] = path.read_bytes()
    for original, target in [('GRAPH_FORMAT.md', 'graph-format.md'), ('TASK_PROTOCOL.md', 'task-protocol.md'), ('NODE_DESIGN.md', 'node-design.md')]:
        path = skill / 'references' / target if packaged else source / original
        content = path.read_text(encoding='utf-8')
        if not packaged:
            for old, new in [('](codemap/', '](../../../runtime/codemap/'),
                             ('](TASK_PROTOCOL.md)', '](task-protocol.md)'),
                             ('](GRAPH_FORMAT.md)', '](graph-format.md)'),
                             ('](NODE_DESIGN.md)', '](node-design.md)'),
                             ('](examples/sample-map.json)', '](sample-map.json)'),
                             ('](skills/repository-blueprint/references/commands.md)', '](commands.md)')]:
                content = content.replace(old, new)
        files[f'skills/{NAME}/references/{target}'] = content.encode('utf-8')
    example = skill / 'references/sample-map.json' if packaged else source / 'examples/sample-map.json'
    files[f'skills/{NAME}/references/sample-map.json'] = example.read_bytes()
    scripts = ['run.py', 'build_plugin.py', 'build_portable.py', 'setup_parsers.py']
    if kind == 'codex':
        scripts.append('install_plugin.py')
    for name in scripts:
        files['scripts/' + name] = (source / 'scripts' / name).read_bytes()
    for name in ['INSTALL.md', 'WORKBENCH.md', 'requirements-parsers.txt', *(['install.ps1'] if kind == 'codex' else [])]:
        files[name] = (source / name).read_bytes()
    return files


def build(output, *, python=sys.executable, update=False, source=ROOT, kind='codex'):
    if kind not in {'codex', 'portable'}:
        raise ValueError('Unknown package kind.')
    source = Path(source).resolve()
    output = Path(output).expanduser().resolve()
    if output.name != NAME:
        raise ValueError(f'Plugin directory must be named {NAME}.')
    if output == source or source.is_relative_to(output):
        raise ValueError('Build output cannot contain or replace the source directory.')
    if output.exists() and any(output.iterdir()):
        manifest = output / '.codex-plugin/plugin.json'
        receipt_path = output / 'blueprint-package.json'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {}
        matches = (manifest.is_file() and json.loads(manifest.read_text(encoding='utf-8'))['name'] == NAME) if kind == 'codex' else (receipt.get('name') == NAME and receipt.get('kind') == 'portable')
        if not update or not matches or (receipt and receipt.get('kind', 'codex') != kind):
            raise ValueError('Existing output requires --update and a matching package kind.')
    python = Path(python).resolve(strict=True)
    files = payload_files(source, kind=kind)
    config = {'mcpServers': {NAME: {'command': str(python),
        'args': ['-u', str(output / 'scripts/run.py'), 'mcp'],
        'env': {'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        'startup_timeout_sec': 30, 'tool_timeout_sec': 120}}}
    if kind == 'codex':
        files['.mcp.json'] = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # Portable configs contain only each client's documented stdio fields.
    runtime = source / 'runtime' if (source / 'runtime/codemap').is_dir() else source
    sys.path.insert(0, str(runtime))
    from codemap.connections import mcp_config
    for client in ('generic', 'claude-code', 'gemini-cli'):
        value = mcp_config(client, python=python, entry=output / 'scripts/run.py')
        files[f'connections/{client}.json'] = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    hashes = {}
    for relative, data in files.items():
        target = output / relative
        if not target.resolve().is_relative_to(output):
            raise ValueError(f'Package target escapes through a link: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    receipt = {'name': NAME, 'kind': kind, 'format_version': 1, 'python': str(python), 'files': hashes}
    (output / 'blueprint-package.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output


def main(kind='codex'):
    parser = argparse.ArgumentParser(description='Build a code-blueprint ' + kind + ' package.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--update', action='store_true')
    parser.add_argument('--zip', type=Path)
    args = parser.parse_args()
    if args.zip and args.zip.exists():
        parser.error('Zip already exists; choose a new filename.')
    output = build(args.output, python=args.python, update=args.update, kind=kind)
    if args.zip:
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        receipt = json.loads((output / 'blueprint-package.json').read_text(encoding='utf-8'))
        with zipfile.ZipFile(args.zip, 'x', zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted([*receipt['files'], 'blueprint-package.json']):
                archive.write(output / relative, NAME + '/' + relative)
    result = {'package': str(output), 'kind': kind, 'zip': str(args.zip.resolve()) if args.zip else None}
    if kind == 'codex':
        result['plugin'] = str(output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
