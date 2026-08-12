# Controlled bulk-ingestion design

This document proposes the next ingestion increment. It has not been run, and
the current controlled database has not been changed.

## Current gaps

The current CLI discovers a directory recursively (optionally by department),
has SHA-256 skip behavior, and stores each document transactionally. It does
not accept an explicit manifest, persist batch progress, isolate per-document
exceptions, or produce structured per-document outcomes. An unhandled PDF
error currently terminates the process. Schema initialization is also coupled
to ingestion startup and should be made an explicit administrative command.

## Proposed interface

Use an explicit UTF-8 CSV or JSONL manifest containing one corpus-relative PDF
path per item. A future command should require the manifest and a stable batch
identifier, for example:

```text
python -m cli.ingest_batch --manifest data/batches/finance_50.csv --batch-id finance-50-v1
```

Paths must resolve inside `GR_ROOT`, have a `.pdf` suffix, and be unique after
case-insensitive normalization. Directory-wide discovery should not be the
default for controlled runs.

## Resume and idempotence

- Compute SHA-256 before processing.
- Skip a SHA already recorded as a successful document unless an explicit,
  reviewed replacement mode is requested.
- Keep an append-only JSONL result ledger under `outputs/ingestion/<batch-id>/`.
- On restart, skip manifest entries whose latest ledger state is `SUCCESS` or
  `WARNING`; retry `FAILURE` only with an explicit retry option.
- Write one document in one PostgreSQL transaction. A failed transaction must
  not affect previous or subsequent documents.
- Do not call schema creation from the batch runner.

## Per-document isolation

Wrap extraction, validation, chunking, embedding, and storage for each PDF in a
single error boundary. Record the failure, release temporary resources, and
continue to the next manifest entry. Handle keyboard interruption separately
so the current ledger entry is flushed and the batch can resume.

## Structured result record

Each manifest entry should produce a JSON object with:

- batch ID, manifest index, relative path, SHA-256
- start/end timestamps and processing duration
- page count, digital-page count, OCR-page count
- raw metadata and GR-number/date/subject validation status and reason
- chunk count and maximum chunk token count
- embedding model and observed dimensions
- `SUCCESS`, `WARNING`, `FAILURE`, or `SKIPPED_EXISTING`
- stage name, exception class, and concise error message on failure

Warnings include suspicious/missing metadata and recoverable extraction-quality
signals. A chunk above the configured maximum or an embedding dimension other
than 1,024 is a hard failure before database writes.

## Controlled rollout gate

For the first 50-document run:

1. Freeze and review the explicit manifest.
2. Record database counts and a corpus-file hash inventory.
3. Run extraction/chunking validation without writes where practical.
4. Run the batch with per-document transactions and the append-only ledger.
5. Review every warning/failure and confirm the token invariant.
6. Record database counts and rerun the frozen retrieval benchmark.

Persisting metadata-validation fields in PostgreSQL is not part of this design
phase. If queryable persistence is required, propose and review a schema
migration before applying it.
