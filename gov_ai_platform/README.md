# Government Administrative AI Platform — Knowledge Search

This phase provides the administrative knowledge brain and a server-rendered
Django semantic evidence-search interface. It reuses the validated BAAI/bge-m3
embeddings, existing PostgreSQL/pgvector database, model cache, and GR corpus.
It does not generate answers with an LLM.

## Local setup

1. Use `D:\LLM_Project_data\venv`; do not create another environment.
2. Copy `.env.example` to `.env` and replace the Django secret for local use.
3. Verify the existing `gr-postgres` container is running on port 5433.
4. Run `python manage.py runserver` from this directory.

Search from the CLI with:

```powershell
python -m cli.search "मराठी प्रश्न" --top-k 5
```

Validate or execute a frozen ingestion manifest with:

```powershell
python -m cli.batch_ingest data/manifests/controlled_10.csv --dry-run
python -m cli.batch_ingest MANIFEST.csv
```

The completed controlled expansion manifest is
`data/manifests/expansion_50_v1.csv`; its evidence bundle is under
`outputs/corpus_expansion/expansion_50_v1/`.

Use `--force` only for an explicit new reprocessing run. Use `--resume` to
continue the latest compatible run, optionally with `--run-id`. See
`docs/BULK_INGESTION_DESIGN.md` for transaction and output details.

Run unit tests with:

```powershell
python -m pytest -q
```

See `docs/ARCHITECTURE.md` for component boundaries and
`docs/CURRENT_STATE.md` for current limitations.
