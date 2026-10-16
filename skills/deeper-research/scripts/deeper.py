#!/usr/bin/env python3
"""Locate the project and exec its CLI; all research policy remains in the CLI."""
import os
from pathlib import Path
import sys


def main() -> int:
    configured = os.environ.get('DEEPER_RESEARCH_ROOT')
    root = Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[3]
    command = root/'.venv/bin/deeper'
    config = root/'config.json'
    if not config.is_file() or not command.is_file() or not os.access(command, os.X_OK):
        print(f'Deeper Research project is not ready at {root}. '
              'Check its installation or set DEEPER_RESEARCH_ROOT to the existing project.', file=sys.stderr)
        return 2
    try:
        os.chdir(root)
        os.execv(str(command), [str(command), '--config', str(config), *(sys.argv[1:] or ['--help'])])
    except OSError as error:
        print(f'Cannot launch the Deeper Research project: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
