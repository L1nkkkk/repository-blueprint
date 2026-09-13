"""Explicit, one-time installation of the pinned optional language backends."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='安装 C++ / C# / TS / JS 本地语法解析器；Python 无需额外依赖。')
    parser.add_argument('--check', action='store_true', help='只检查，不安装')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / 'runtime' if (root / 'runtime/codemap').is_dir() else root))
    if not args.check:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--only-binary=:all:',
                        '-r', str(root / 'requirements-parsers.txt')], check=True)
    from codemap.syntax import capabilities
    result = capabilities()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(item['available'] for item in result.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
