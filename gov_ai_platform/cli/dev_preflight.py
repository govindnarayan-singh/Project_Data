"""Cheap developer preflight. No model load, benchmark, migration, or DB writes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from cli.native_postgres import NativePostgresError, ensure_native_postgres_running, native_postgres_status
from knowledge_engine.settings import PROJECT_ROOT

REPOSITORY_ROOT = Path('D:/LLM_Project_data')
APPLICATION_ROOT = REPOSITORY_ROOT / 'gov_ai_platform'


def workspace_preflight(write_paths: list[str] = ()) -> dict:
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=PROJECT_ROOT,
                            env=env, capture_output=True, text=True, check=True, timeout=5)
    repository = Path(result.stdout.strip()).resolve()
    application = PROJECT_ROOT.resolve()
    cwd = Path.cwd().resolve()
    paths = [Path(path).resolve() for path in write_paths]
    valid = (repository == REPOSITORY_ROOT.resolve() and application == APPLICATION_ROOT.resolve()
             and cwd.is_relative_to(application) and all(path.is_relative_to(application) for path in paths))
    if not valid:
        raise NativePostgresError('Workspace guard failed: parent Git root and application-local cwd/write paths required')
    return {'workspace_guard_status': 'PASS', 'repository_root': str(repository),
            'application_root': str(application), 'cwd': str(cwd), 'python': sys.executable,
            'checked_write_paths': [str(path) for path in paths]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ensure-postgres', action='store_true', help='Permit normal existing-cluster startup')
    parser.add_argument('--write-path', action='append', default=[], help='Validate an intended application write path')
    args = parser.parse_args()
    try:
        report = workspace_preflight(args.write_path)
        report['postgresql'] = ensure_native_postgres_running() if args.ensure_postgres else native_postgres_status()
        print(json.dumps(report, indent=2))
        if report['postgresql']['status'] != 'READY':
            raise SystemExit(1)
    except (NativePostgresError, OSError, subprocess.SubprocessError) as exc:
        print(f'Preflight: {exc}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
