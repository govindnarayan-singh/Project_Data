from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from knowledge_engine.backup import BackupError, BackupService
from knowledge_engine.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and restore-verify a sovereign PostgreSQL backup")
    parser.add_argument("--output-root", type=Path, default=settings.backup_root)
    parser.add_argument("--label", default="")
    parser.add_argument("--verify", action="store_true", help="Recompute SHA-256 for all local GR source files")
    args = parser.parse_args()
    try:
        result = BackupService(output_root=args.output_root).create_snapshot(label=args.label, verify=args.verify)
    except (BackupError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"BACKUP FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
