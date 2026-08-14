# Restore snapshot `20260814T213358+0530_before-v2-1-retry`

The custom-format `database/grdb.dump` is authoritative. Keep `globals.sql`
protected because role dumps can contain password hashes. Never restore over a
running production database without an approved outage and a separate backup.

```powershell
podman start gr-postgres
podman cp database/globals.sql gr-postgres:/tmp/globals.sql
podman exec gr-postgres psql -U gr_user -d postgres -f /tmp/globals.sql
podman exec gr-postgres createdb -U gr_user -T template0 grdb
podman cp database/grdb.dump gr-postgres:/tmp/grdb.dump
podman exec gr-postgres pg_restore -U gr_user -d grdb --no-owner --no-privileges --exit-on-error /tmp/grdb.dump
podman exec gr-postgres psql -U gr_user -d grdb -c "SELECT extname,extversion FROM pg_extension ORDER BY extname;"
```

Configure `GR_ROOT` and `EXTERNAL_DOCUMENT_ROOT` to the permanent source roots,
verify the counts in `metadata/database_counts.json`, run `python manage.py
check`, then start Django. Do not copy source PDFs into the database snapshot.
