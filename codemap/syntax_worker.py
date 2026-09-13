"""Private isolated syntax worker; receives text, never opens the target source."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codemap.syntax import parse


if __name__ == '__main__':
    packet = json.load(sys.stdin)
    json.dump(parse(packet['source'], packet['text']), sys.stdout, ensure_ascii=False)
