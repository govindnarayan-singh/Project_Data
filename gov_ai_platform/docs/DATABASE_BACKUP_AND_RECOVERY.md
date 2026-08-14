# Database backup and disaster recovery

## Normal database runtime

The normal application database is native Windows PostgreSQL 17.11 with
pgvector 0.8.6. It listens only on `127.0.0.1:5434` and uses:

```dotenv
DATABASE_URL=postgresql://gr_user:<local-secret>@127.0.0.1:5434/grdb
POSTGRES_BACKUP_MODE=native
POSTGRES_BIN=D:/LLM_Project_data/PostgreSQL/17/pgsql/bin
POSTGRES_DATA=D:/LLM_Project_data/PostgreSQL/data17
```

Podman is rollback-only. Do not start, stop, restore, or back up the normal
database through the old `gr-postgres` container.

Use the project command after a Windows restart:

```powershell
cd D:\LLM_Project_data\gov_ai_platform
& D:\LLM_Project_data\venv\Scripts\python.exe -m cli.native_postgres status
& D:\LLM_Project_data\venv\Scripts\python.exe -m cli.native_postgres start
```

`stop` performs a PostgreSQL fast, waited shutdown. It must be used only for a
planned local maintenance window.

## Proven backup boundary

`python -m cli.backup_database --verify` invokes the configured native
PostgreSQL 17 tools and creates a timestamped logical snapshot under
`BACKUP_ROOT` (default `D:/LLM_Project_data/Backups/gov_ai_platform`).

Each snapshot contains the custom-format `database/grdb.dump`, protected
`globals.sql`, readable `schema.sql`, counts and extension versions, SHA-256
checksums, and inventories for both permanent PDF roots. Do not expose
`globals.sql`; a role dump can contain password hashes.

```powershell
& D:\LLM_Project_data\venv\Scripts\python.exe -m cli.backup_database `
  --label before-change --verify
```

The command rejects unsafe destinations including drive roots, the repository,
source-document roots, and Incoming. It never restores over `grdb` and never
prunes snapshots.

## Verified native snapshot

The first post-migration snapshot is:

`D:/LLM_Project_data/Backups/gov_ai_platform/20260814T223701+0530_native-windows-v1`

Its 10,763,722-byte `grdb.dump` has SHA-256
`f3407b82d6922c00b899186e5bcfb8469d46f0716fb4e93c364912e381bc6c86`.
Archive inspection found 185 entries, pgvector, and all 15 required tables.
The isolated restore reproduced 71 documents, 1,322 pages, 1,237 chunks,
1,237 embeddings of 1,024 dimensions, 1,408 KG candidates, 88 relations,
17 REFERENCES, 71 ISSUED_BY, 2,715 local-catalogue rows, and the snapshot-time
63 external-catalogue rows. Vector self-distance was zero. The temporary
restore database was removed and production was unchanged.

## Native recovery

Install or unpack a compatible PostgreSQL 17 runtime and install the compatible
pgvector extension into it. Initialize an empty cluster if the current data
directory is lost. Review `globals.sql` before restoring roles; do not blindly
overwrite an existing role.

From the snapshot directory, create a fresh database owned by `gr_user`, then:

```powershell
$pgbin = 'D:\LLM_Project_data\PostgreSQL\17\pgsql\bin'
& "$pgbin\createdb.exe" -h 127.0.0.1 -p 5434 -U gr_user -T template0 grdb
& "$pgbin\pg_restore.exe" -h 127.0.0.1 -p 5434 -U gr_user -d grdb `
  --no-owner --no-privileges --exit-on-error database\grdb.dump
& "$pgbin\psql.exe" -h 127.0.0.1 -p 5434 -U gr_user -d grdb `
  -c "SELECT extname,extversion FROM pg_extension ORDER BY extname;"
```

Restore source collections separately at their inventory-matched locations:

```dotenv
GR_ROOT=D:/LLM_Project_data/GRs
EXTERNAL_INCOMING_ROOT=D:/LLM_Project_data/Government_Documents/Incoming
EXTERNAL_DOCUMENT_ROOT=D:/LLM_Project_data/Government_Documents/External_Official
```

Compare source files with `inventories/*.csv` and
`metadata/checksums.sha256`, run `python manage.py check`, and verify exact
corpus/vector counts before starting Django. Incoming is staging evidence, not
permanent authoritative custody.
