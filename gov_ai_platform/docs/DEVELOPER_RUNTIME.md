# Developer runtime

From `D:\LLM_Project_data\gov_ai_platform`, using the existing environment:

```bash
source /d/LLM_Project_data/venv/Scripts/activate
python manage.py runserver
```

Runserver calls `cli.native_postgres.ensure_native_postgres_running()` before
Django starts. This is also the single implementation behind:

```powershell
python -m cli.native_postgres status
python -m cli.native_postgres ensure
python -B -m cli.dev_preflight --ensure-postgres --write-path outputs/my_task
```

`status` and preflight without `--ensure-postgres` never start PostgreSQL.
`start` aliases `ensure`; `stop` is for explicit maintenance only. Django exit
does not stop the shared database. No Windows service or boot task is installed:
startup is on demand when runserver or ensure is invoked.

## Configuration and identity

Existing `.env` credentials remain private; process environment takes precedence.
`AUTO_START_POSTGRES=false` disables the runserver hook. Unset defaults to enabled
only with local `DJANGO_DEBUG=true` (the existing default) and no active `CI` flag.
Explicit true opts in even in CI. Other Django commands and runserver help never
autostart. Production WSGI/ASGI imports do not invoke the hook.

The helper accepts only `127.0.0.1:5434/grdb`, the existing native binaries under
`D:\LLM_Project_data\PostgreSQL\17\pgsql\bin`, `data17` and `logs`. It checks
the cluster's PG_VERSION, uses pg_isready, then connects read-only to verify
server version 17.x, database, port and data directory. If the SQL data-directory
setting is not visible to the application role, it additionally requires
pg_ctl status for the exact existing data directory and reports that limitation.
Credentials never enter subprocess arguments or normal/error output.

A healthy ensure performs no start/stop. A cold ensure uses an application-local
OS-held startup lock; competing processes recheck readiness after acquiring it.
The lock releases on process exit, including crashes. This also makes repeated
Django autoreloader calls safe. Startup uses a 30-second deadline with bounded
tool/connection/query calls (an in-flight identity check can add a few seconds).
Failure reports only a redacted, size-limited last eight startup-log lines.
Do not delete PostgreSQL PID files or kill an unrecognized port owner to unblock it.

## Write boundary

Git root is `D:\LLM_Project_data`; application and all task project files stay
under `D:\LLM_Project_data\gov_ai_platform`. Normal operation of the existing
PostgreSQL 17 instance may write runtime/database state to
`D:\LLM_Project_data\PostgreSQL\data17` and logs to
`D:\LLM_Project_data\PostgreSQL\logs`. This is not permission to initialize,
recreate, move, restore or upgrade the cluster, alter backups, install software,
or touch PostgreSQL 13 on 5432. Evaluation/read-only SQL rules still apply.

## Efficient validation

Use preflight once at task start, scoped reads thereafter, and rerun only when
relevant state changes. The command validates Git/application/cwd and supplied
write paths, and reports verified runtime identity without model loading or
corpus scans. It does not enforce filesystem permissions or replace mandatory
task-specific verification. AGENTS.md holds the short execution policy in the
application instruction scope, following [official AGENTS.md guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

For runtime changes run `tests/test_native_postgres.py`, `manage.py check`, a
cold-start/repeated-ensure check, and a runserver HTTP smoke check. Keep pytest's
temporary directory under the application; ensure its parent exists. Do not
rerun dense, graph, or locked-test benchmarks for runtime-only edits. No new
dependency is needed.
