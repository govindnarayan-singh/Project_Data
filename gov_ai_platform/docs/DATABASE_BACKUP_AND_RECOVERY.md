# Database backup and disaster recovery

## Proven backup boundary

`python -m cli.backup_database --verify` creates a timestamped logical snapshot
under configured `BACKUP_ROOT` (default
`D:/LLM_Project_data/Backups/gov_ai_platform`). The destination is ordinary
Windows D: storage outside the Podman container, named volume, container
filesystem, and WSL virtual disk.

Each snapshot contains the authoritative custom-format `database/grdb.dump`
(including pgvector embeddings and all application tables), protected
`globals.sql`, readable `schema.sql`, counts and extension versions, SHA-256
checksums, and inventories for both permanent PDF roots. Do not expose
`globals.sql`; a role dump can contain password hashes.

```powershell
cd D:\LLM_Project_data\gov_ai_platform
& D:\LLM_Project_data\venv\Scripts\python.exe -m cli.backup_database --label before-change --verify
```

`--output-root` may select another safe absolute location. The command rejects
the project root, source-document roots, drive roots, and quarantine. It never
restores over `grdb` and never prunes snapshots. `BACKUP_KEEP_LAST` is reserved
for future retention; `0` means no pruning and no automatic deletion exists.

## Verified recovery rehearsal

Every successful backup runs `pg_restore --list`, requires the vector extension
and all critical tables/table-data entries, creates a strictly named
`grdb_restore_check_<timestamp>_<random>` database, restores the custom dump,
and verifies counts, FK projections, duplicate identities, relation evidence,
vector dimensions, and vector self-distance. It then drops only that database
and proves production counts/fingerprint did not change. A snapshot is not a
PASS unless `RESTORE_VERIFIED` is true.

The 2026-08-14 rehearsal restored 71 documents, 1,322 pages, 1,237 chunks and
embeddings at 1,024 dimensions, 1,408 KG candidates, 88 relations, 17
REFERENCES, 71 ISSUED_BY, 2,715 local-catalogue rows, and 63 external-catalogue
rows. The temporary database was removed.

## Recovery after container loss

Use a compatible PostgreSQL 17 + pgvector image. Obtain passwords through the
approved local secret mechanism; never put a real password in this document or
the repository.

```powershell
podman run -d --name gr-postgres -p 5433:5432 `
  -e POSTGRES_USER=restore_admin -e POSTGRES_DB=postgres `
  -e POSTGRES_PASSWORD=<approved-secret-injection> `
  docker.io/pgvector/pgvector:pg17
podman cp database/globals.sql gr-postgres:/tmp/globals.sql
podman exec gr-postgres psql -U restore_admin -d postgres -f /tmp/globals.sql
podman exec gr-postgres createdb -U restore_admin -T template0 -O gr_user grdb
podman cp database/grdb.dump gr-postgres:/tmp/grdb.dump
podman exec gr-postgres pg_restore -U restore_admin -d grdb `
  --no-owner --no-privileges --exit-on-error /tmp/grdb.dump
podman exec gr-postgres psql -U restore_admin -d grdb `
  -c "SELECT extname,extversion FROM pg_extension ORDER BY extname;"
podman exec gr-postgres psql -U restore_admin -d grdb `
  -c "SELECT (SELECT count(*) FROM documents) documents,(SELECT count(*) FROM pages) pages,(SELECT count(*) FROM chunks) chunks,(SELECT count(*) FROM chunks WHERE vector_dims(embedding)=1024) vectors_1024;"
```

If `gr_user` already exists, review `globals.sql` and do not blindly replay
conflicting role creation. The database dump remains authoritative when globals
must be restored selectively.

Restore source collections separately at their inventory-matched locations;
they are intentionally not duplicated inside each database snapshot:

```dotenv
GR_ROOT=D:/LLM_Project_data/GRs
EXTERNAL_DOCUMENT_ROOT=D:/LLM_Project_data/Government_Documents/External_Official
BACKUP_ROOT=D:/LLM_Project_data/Backups/gov_ai_platform
DATABASE_URL=postgresql://gr_user:<local-secret>@localhost:5433/grdb
```

Compare source files with `inventories/*.csv` and
`metadata/checksums.sha256`, run `python manage.py check`, then start Django.
Never treat quarantine as permanent custody.
