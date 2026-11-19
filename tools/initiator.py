#!/usr/bin/env python3
"""Entry point used by Codex: python -B tools/initiator.py open --request request.json."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initiator.cli import main

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        import json
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
