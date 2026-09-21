# Working agreements

- Preserve the original GR PDFs; never modify corpus files.
- Do not ingest the full 2,715-document corpus without explicit instruction.
- Use native Windows PostgreSQL 17 + pgvector on `127.0.0.1:5434` as the normal runtime; keep `gr-postgres` rollback-only.
- Do not change the BAAI/bge-m3 embedding model or its 1,024 dimensions without approval.
- Preserve page-level traceability from every chunk to its source PDF.
- Never silently change retrieval-evaluation ground truth.
- Run relevant tests after modifications.
- Prefer deterministic metadata and reference parsing where practical.
- Do not introduce LLMs into extraction logic.
- Explain proposed schema changes before applying them.
- Keep web concerns separate from the knowledge engine.

## Workspace and native runtime

- Git repository root: `D:\LLM_Project_data`. Application/project-write root:
  `D:\LLM_Project_data\gov_ai_platform`. The parent Git root is intentional;
  never require a nested `.git`, copy the repo, or initialize another one.
- Keep source, tests, docs, outputs and task temporary files inside the application.
  Use Git for inspection with `GIT_OPTIONAL_LOCKS=0`; preserve unrelated changes.
- Normal operation of the EXISTING native PostgreSQL 17 cluster may write to
  `D:\LLM_Project_data\PostgreSQL\data17` and
  `D:\LLM_Project_data\PostgreSQL\logs`. Starting/stopping that runtime is
  authorized; do not stop a task merely because startup creates WAL/PID/log files.
  This exception does not authorize initdb, a new cluster, restore, database
  deletion, installations, or touching PostgreSQL 13 / port 5432. Task-specific
  read-only database restrictions still apply to application SQL.
- Reuse `cli.native_postgres.ensure_native_postgres_running()`; do not duplicate
  startup logic. `python manage.py runserver` calls it automatically in local
  development. `AUTO_START_POSTGRES=false` opts out; other Django commands do
  not autostart. Use the existing `D:\LLM_Project_data\venv` environment.

## Efficient task execution

- Read the current request and this file once, then inspect relevant source and
  targeted documentation. Follow explicit read-first instructions; do not reread
  unchanged long documents every phase or replay completed phases after resuming.
- Once per task, use `python -B -m cli.dev_preflight --ensure-postgres` when the
  task needs the database, otherwise use scoped workspace inspection. The flag
  permits startup, not migrations or corpus mutation. `--write-path PATH` checks
  planned application paths. The preflight is not a filesystem sandbox.
- Start with `rg` and bounded reads. Summarize decisions and blockers briefly;
  avoid repeated broad inventories, full log dumps and duplicate reports.
- Run focused tests for changed behavior and one relevant smoke check. Broaden
  only for affected contracts, failures, or explicit task requirements. Runtime-
  only work needs runtime unit tests, one live ensure/idempotence check, and Django
  startup/check, not model loading or dense/KG/locked benchmark reruns.
- Do not resume stopped workstreams (including Graph Retrieval V3) without the
  current request. Preserve frozen experiment files and one-shot test ledgers.
- No global/system installs or dependency additions without explicit approval.
  Keep Python bytecode and test caches/temp files inside the application (use
  `-B`, app-local `--basetemp`, and `-p no:cacheprovider` for bounded test runs).
