"""Keep native parser failures and timeouts outside the Agent/canvas process."""

import json
from pathlib import Path
import subprocess
import sys

from . import syntax


def parse(source, text):
    engine = syntax.backend(source['language'], source['path'])
    if source['language'] == 'python' or not engine['available']:
        return syntax.parse(source, text)
    try:
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).with_name('syntax_worker.py'))],
                                 input=json.dumps({'source': source, 'text': text}, ensure_ascii=False),
                                 capture_output=True, text=True, encoding='utf-8', timeout=15,
                                 cwd=Path(__file__).parent, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise ValueError(f'Native parser exited with code {process.returncode}; file requires review.')
        result = json.loads(process.stdout)
        if result.get('sha256') != source['sha256'] or result.get('parser_id') != engine['id']:
            raise ValueError('Parser result does not match the requested source/backend.')
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        message = 'Native parser exceeded 15 seconds; file requires review.' if isinstance(error, subprocess.TimeoutExpired) else str(error)
        return {'source_id': source['id'], 'path': source['path'], 'sha256': source['sha256'],
                'language': source['language'], 'parser_id': engine['id'], 'state': 'unavailable',
                'symbols': [], 'sites': [], 'diagnostics': [{'message': message, 'line': None}],
                'diagnostic_count': 1, 'limitations': 'Native syntax extraction failed. No review progress was recorded.'}
