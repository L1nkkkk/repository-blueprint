"""Relocatable entry point shipped with the plugin. No checkout/PYTHONPATH needed."""
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    raise SystemExit('代码蓝图需要 Python 3.10+。请重新运行安装程序并指定 -Python。')
root = Path(__file__).resolve().parent.parent
runtime = root / 'runtime' if (root / 'runtime/codemap').is_dir() else root
sys.path.insert(0, str(runtime))
from codemap.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())
