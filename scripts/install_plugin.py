"""Install/update our personal Codex plugin through supported local helpers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from build_plugin import build, payload_files, NAME


def run(*command):
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8',
                            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description='Install 代码蓝图 into the personal Codex marketplace.')
    parser.add_argument('--codex', default=shutil.which('codex'))
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or newer is required.')
    if not args.codex:
        parser.error('Codex CLI was not found. Pass -Codex to install.ps1.')
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    helpers = codex_home / 'skills/.system/plugin-creator/scripts'
    required = ['create_basic_plugin.py', 'read_marketplace_name.py', 'update_plugin_cachebuster.py']
    if not all((helpers / name).is_file() for name in required):
        parser.error('Codex Plugin Creator helpers are missing. Enable/update the built-in Plugin Creator skill first.')
    target = Path.home() / 'plugins' / NAME
    marketplace = Path.home() / '.agents/plugins/marketplace.json'
    known = False
    marketplace_name = 'personal'
    if marketplace.exists():
        marketplace_name = run(sys.executable, str(helpers / 'read_marketplace_name.py'))
        entries = json.loads(marketplace.read_text(encoding='utf-8'))['plugins']
        entry = next((e for e in entries if e['name'] == NAME), None)
        if entry:
            if entry['source'] != {'source': 'local', 'path': f'./plugins/{NAME}'}:
                parser.error('Existing marketplace entry points elsewhere; installation made no changes.')
            known = True
    if target.exists() and not (target / 'blueprint-package.json').is_file():
        parser.error('Target directory exists without a code-blueprint package receipt; installation made no changes.')
    payload_files()
    # Installation is explicit; indexing never downloads or executes repository code.
    run(sys.executable, str(Path(__file__).with_name('setup_parsers.py')))
    if not known:
        if target.exists():
            parser.error('An existing package has no marketplace entry; resolve registration before updating.')
        run(sys.executable, str(helpers / 'create_basic_plugin.py'), NAME,
            '--with-skills', '--with-scripts', '--with-mcp', '--with-marketplace')
        marketplace_name = run(sys.executable, str(helpers / 'read_marketplace_name.py'))
    build(target, update=True)
    if known:
        run(sys.executable, str(helpers / 'update_plugin_cachebuster.py'), str(target))
    import hashlib
    receipt_path = target / 'blueprint-package.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    receipt['files']['.codex-plugin/plugin.json'] = hashlib.sha256((target / '.codex-plugin/plugin.json').read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    subprocess.run([sys.executable, str(target / 'scripts/run.py'), '--help'], cwd=Path.home(),
                   check=True, stdout=subprocess.DEVNULL)
    print(run(args.codex, 'plugin', 'add', NAME + '@' + marketplace_name, '--json'))
    print('代码蓝图已安装。请在新任务中使用插件；现有地图和布局保留。')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode)
