# Controlled bulk-ingestion design

This design is implemented for controlled ingestion. The frozen 10-document
evaluation corpus was regenerated successfully; no new documents were added.

## Interface and manifest

The production path is manifest-only and never uses directory ordering or
`--limit`:

```powershell
python -m cli.batch_ingest MANIFEST.csv --dry-run
python -m cli.batch_ingest MANIFEST.csv
python -m cli.batch_ingest MANIFEST.csv --force
python -m cli.batch_ingest MANIFEST.csv --resume
python -m cli.batch_ingest MANIFEST.csv --resume --run-id RUN_ID
```

CSV requires `file_path` and `department`; `document_label` and `notes` are
optional. Paths must resolve inside `GR_ROOT`, have a `.pdf` suffix, exist, and
be unique after case-insensitive normalization. One invalid row rejects the
manifest before processing.

Default mode ingests new or changed sources and skips unchanged SHA-256
identities. `--force` explicitly regenerates unchanged documents. `--resume`
selects the latest compatible non-dry-run by manifest hash unless a run ID is
given. Resume and force cannot be combined: resume finishes pending work;
force starts a separately auditable run.

Dry-run validates the manifest, paths, SHA-256 identities, and planned
operations. It never extracts, OCRs, embeds, or writes PostgreSQL.

## Resume and idempotence

- Compute SHA-256 before processing.
- Resolve identity by source path or SHA-256; conflicting identities fail.
- Skip unchanged successful documents unless `--force` is explicit.
- Checkpoint `results.csv` and `summary.json` atomically after every row under
  `outputs/bulk_ingestion/<run-id>/`.
- On resume, retain completed rows whose current source SHA-256 matches and
  retry unfinished or failed rows.
- Never call schema creation from the batch runner.

## Per-document isolation and model lifecycle

Extraction, metadata validation, chunking, embedding, and persistence have
separate failure statuses. A document failure is recorded and later manifest
rows continue. Shared OCR/database availability and BGE-M3 initialization are
checked once as an infrastructure preflight when processing is required.
Unchanged skip-only runs do not load OCR/model dependencies.

## Transaction strategy

All expensive work completes before persistence. A single transaction then:

1. Locks the matching path/SHA identity row with `FOR UPDATE`.
2. Preserves its document ID or inserts one new row.
3. Updates document fields and replaces pages and chunks.
4. Validates expected counts, contiguous pages, page references, the 700-token
   limit, and 1,024-dimensional embeddings.
5. Commits only after every check passes.

PostgreSQL MVCC leaves the previous committed version searchable until commit.
Any persistence or integrity error rolls back the entire replacement.

## Structured outputs

Each run contains:

- `manifest_snapshot.csv`
- `results.csv`
- `summary.json`
- `errors.csv` when failures occur

Per-document data includes run/path/SHA identity, operation and status,
timings, page and OCR counts, raw metadata plus validation results, chunk
statistics, embedding count/dimension, and error stage/message.

Supported terminal statuses include `SUCCESS`, `SUCCESS_WITH_WARNING`,
`SKIPPED_UNCHANGED`, `FAILED_EXTRACTION`, `FAILED_METADATA`,
`FAILED_CHUNKING`, `FAILED_EMBEDDING`, and `FAILED_DATABASE`. Suspicious or
missing metadata normally produces a warning, not a hard failure.

## Controlled 50-new-document rollout gate

1. Freeze and review an explicit 50-path manifest.
2. Record database counts and corpus hashes.
3. Run default and forced dry-plans as appropriate.
4. Run complete tests.
5. Execute with per-document transactions and checkpoints.
6. Review every warning/failure and confirm token/dimension invariants.
7. Record database counts and rerun the frozen benchmark.

Metadata-validation results remain application-level. Queryable persistence
requires a separately reviewed schema migration.

The `expansion_50_v1.csv` gate completed on 2026-08-12. All 50 frozen new
documents succeeded, adding 455 pages and 411 chunks. Global integrity after
the run was 61 documents, 1,022 pages, 740 chunks/embeddings, zero oversized
chunks, zero wrong-dimension or missing embeddings, and zero duplicate paths or
SHA-256 values. A repeat run without `--force` skipped all 50 documents.
