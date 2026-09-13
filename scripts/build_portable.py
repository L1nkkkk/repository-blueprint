"""Build the standalone MCP/Skill/canvas distribution, without a Codex install."""
from build_plugin import main

if __name__ == '__main__':
    main(kind='portable')
