from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from knowledge_engine.settings import settings


def _pg_ctl() -> Path:
    executable = settings.postgres_bin / "pg_ctl.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"native pg_ctl not found: {executable}")
    if not settings.postgres_data.is_dir():
        raise FileNotFoundError(f"native PostgreSQL data directory not found: {settings.postgres_data}")
    return executable


def _run(action: str) -> dict[str, object]:
    pg_ctl = _pg_ctl()
    base = [str(pg_ctl), "-D", str(settings.postgres_data)]
    if action == "status":
        command = [*base, "status"]
    elif action == "start":
        command = [
            *base,
            "-l",
            str(settings.postgres_data / "server.log"),
            "start",
            "-w",
            "-t",
            "60",
        ]
    else:
        command = [*base, "stop", "-m", "fast", "-w", "-t", "60"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=70, check=False)
    result = {
        "action": action,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "returncode": completed.returncode,
        "postgres_bin": str(settings.postgres_bin),
        "postgres_data": str(settings.postgres_data),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the native Windows PostgreSQL runtime")
    parser.add_argument("action", choices=("status", "start", "stop"))
    args = parser.parse_args()
    print(json.dumps(_run(args.action), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
