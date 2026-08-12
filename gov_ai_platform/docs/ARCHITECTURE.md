# Architecture

The application is split at a deliberate boundary between web delivery and
knowledge retrieval:

```text
Browser
  → Django search view
  → RetrievalService
  → lazy BGE-M3 embedding provider
  → read-only PostgreSQL/pgvector repository
  → Top-K evidence passages
  → safe PDF viewer by document ID
```

Django views validate HTTP input and render templates. They do not create
embeddings or issue vector SQL. `RetrievalService` owns query validation and
returns typed `SearchResult` objects. The embedding provider loads BGE-M3 once
per process on first search. The repository retains the validated cosine
distance query and existing pgvector schema.

## Ingestion hardening boundaries

The ingestion path remains deterministic:

```text
PDF extraction
  → raw metadata extraction
  → application-level metadata validation
  → token-bounded chunking
  → BGE-M3 embeddings
  → PostgreSQL storage
```

Metadata validation never rewrites extracted values. Each GR number, GR date,
and subject receives a `VALID`, `SUSPICIOUS`, or `MISSING` result with the raw
value and a reason. These validation results are currently application-level;
persisting and querying them would require a reviewed schema migration.

Chunking counts fully assembled text, including separators. Oversized atomic
text is split deterministically at source-character boundaries using verified
tokenizer counts. Final chunk emission enforces
`token_count <= CHUNK_MAX_TOKENS` while retaining page ranges and source text.

## Manifest batch ingestion

Controlled ingestion is an application service in
`knowledge_engine.pipelines.batch_ingest`; `cli.batch_ingest` is its command
adapter. The manifest fixes document membership and order. Paths are resolved
inside `GR_ROOT` and rejected if missing, outside the root, or not PDFs.

For each document, SHA-256 identity is checked before expensive processing.
Extraction, validation, chunking, and embedding occur before the write
transaction. That transaction locks the identity row, preserves its document
ID, updates document fields, replaces pages and chunks, validates page
continuity, token limits, page references, and embedding dimensions, and only
then commits. PostgreSQL MVCC keeps the prior searchable state visible until
commit; an exception rolls back the entire replacement.

BGE-M3 is loaded once during live preflight when at least one manifest row
requires processing. Results are checkpointed after every row under
`outputs/bulk_ingestion/<run-id>/` for safe resume.

Batch results also record extraction, embedding, database, and whole-document
elapsed times. This is reporting instrumentation only and does not alter the
pipeline, model, schema, or transaction boundary.

The PDF endpoint accepts only a numeric database document ID. Its database
path is resolved against `GR_ROOT`, checked to remain under that root, checked
for a `.pdf` suffix, and streamed inline. Filesystem paths are not accepted
from the browser.

## Knowledge Graph V1 boundary

Knowledge Graph V1 is a separate deterministic application path:

```text
Documents + final stored pages
  → production ReferenceParser
  → provenance-first candidate store
  → deterministic resolver + quality overrides
  → validated REFERENCES / ISSUED_BY relations
  → read-only GraphService
  → server-rendered document detail
```

The parser never resolves; the resolver never uses embeddings or subject
similarity; and Django never accesses graph SQL directly. Raw evidence and
normalized values coexist. Unresolved and ambiguous references remain stored
candidates. Strong administrative relation signals cannot enter the validated
edge table unless HUMAN_CONFIRMED.

The graph uses additive PostgreSQL tables described in
`docs/KNOWLEDGE_GRAPH_SCHEMA.md`. The old zero-row `gr_relations` table remains
deprecated and unchanged. Search result cards may link to graph detail, but
dense retrieval SQL, cosine similarity, Top-K behavior, and BGE-M3 are unchanged.
