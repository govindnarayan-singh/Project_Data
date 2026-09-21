#!/usr/bin/env python
from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1 and sys.argv[1] == "runserver" and not {"--help", "-h"}.intersection(sys.argv[2:]):
        from cli.native_postgres import (
            NativePostgresError, READY_MESSAGE, auto_start_enabled,
            ensure_native_postgres_running,
        )
        try:
            if auto_start_enabled():
                ensure_native_postgres_running()
                print(READY_MESSAGE, flush=True)
        except NativePostgresError as exc:
            print(f"PostgreSQL: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
