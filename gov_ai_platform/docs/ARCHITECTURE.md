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
candidates. Strong administrative relations enter the edge table only through
HUMAN_CONFIRMED in strict mode or the later explicit, uniquely resolved,
provenance-linked AUTO_VERIFIED development policy.

The graph uses additive PostgreSQL tables described in
`docs/KNOWLEDGE_GRAPH_SCHEMA.md`. The old zero-row `gr_relations` table remains
deprecated and unchanged. Search result cards may link to graph detail, but
dense retrieval SQL, cosine similarity, Top-K behavior, and BGE-M3 are unchanged.

## Administrative portal

The production-oriented Django surface is a server-rendered, progressively
enhanced three-panel workspace. Read-only web projections live in
`knowledge_engine.portal`; views contain no SQL. BGE-M3 remains lazy and is only
reached through `RetrievalService` after a valid search POST. Document, graph,
corpus-health, and evaluation GET routes use PostgreSQL projections and fixed
artifacts only. See `docs/UI_ARCHITECTURE.md`.

## Graph review automation and acquisition

`KnowledgeGraphValidationPolicy` is the configurable trust boundary for strict
versus automatic development review. Machine decisions remain AUTO_VERIFIED;
only actual review may create HUMAN_CONFIRMED. Strong relations require an
explicit approved phrase, unique identifier resolution, quality gates, and
candidate/evidence provenance.

```text
KG unresolved candidates
  → Acquisition Resolver
  → remaining local GR corpus
  → graph-guided manifest
  → future controlled ingestion
```

`knowledge_engine.acquisition` is separate from ingestion. It builds a safe
SHA-aware inventory, performs limited digital metadata probes, consolidates
targets, resolves only deterministic identity matches, ranks transparently,
and freezes a manifest. It never OCRs the full corpus, chunks, embeds, or writes
document rows.

## Local corpus identity layer

The identity layer separates an ingested document (`documents` plus pages,
chunks, and BGE-M3 vectors), a local catalogued document (identity metadata
only), and an unresolved external document (preserved KG evidence with no
deterministic local identity).

All 2,715 PDFs are represented in PostgreSQL `local_document_catalog` by safe
relative path, SHA-256, file facts, raw/normalized administrative identity,
date, department, kind, subject preview, evidence page/method, quality, and
version. Digital probing is bounded to five early pages; Tesseract `mar+eng` is
used only for unusable early headers and is bounded to three pages. Catalogue
rows never enter retrieval.

KG candidates may point to a real ingested target or a non-search catalogue
target. Local targets remain machine-labelled and cannot create an authoritative
edge until ingestion. `KG_AUTO_INGEST_RESOLVED=false` is a future restartable
batch option and remains disabled.

PostgreSQL remains the V1 graph system of record because graph scale is small,
provenance and administrative metadata are transactional here, pgvector already
lives here, and traversal is simple. Neo4j would introduce dual-write and
synchronization risk. Evaluate it only after measured need: tens/hundreds of
thousands of validated edges, frequent multi-hop queries, a demonstrated
PostgreSQL traversal bottleneck, or required interactive graph algorithms.

## Local closure and external acquisition V1

The controlled loop is now `RESOLVED_LOCAL target -> frozen manifest -> atomic
ingestion -> deterministic KG re-resolution -> AUTO_VERIFIED REFERENCES with
evidence`. Catalogue rows remain non-search records.

External acquisition is a separate security boundary under
`knowledge_engine.external_acquisition`. Its PostgreSQL catalogue records
discovery and provenance but never creates documents, chunks, vectors, or
edges. Sources come from an explicit official registry and queries are
deterministic. Future authorized downloads go first to
`data/acquisition/quarantine` and must pass HTTPS/domain, redirect, timeout,
size, content-type, PDF-signature, filename, hash, and identity gates. See
`docs/EXTERNAL_ACQUISITION.md`.

External Acquisition V1.1 proves the live legal-document path:

```text
Frozen unresolved target
  → document-kind strategy
  → configured official adapter
  → minimal public identity query
  → official result and canonical landing page
  → official PDF in quarantine
  → parsed PDF title/year identity probe
  → AUTO_VERIFIED_READY manifest (not ingestion)
```

The network boundary also validates DNS-resolved addresses against SSRF,
applies per-source rate/retry policies, and uses atomic discovery/download
caches. The live five-file manifest changes only known availability; search
storage, embeddings, KG edges, and dense retrieval remain unchanged.
## External official document ingestion V1 (2026-08-14)

Administrative documents now carry an explicit `source_origin`: `LOCAL_ORIGINAL` or `EXTERNAL_OFFICIAL`. The stored `file_path` is relative to the approved root for that origin; it is never sufficient by itself to infer trust or provenance. Backend PDF resolution rejects absolute paths, traversal, unknown origins, and non-PDF paths. Local originals remain under `GR_ROOT`; verified external documents resolve from `EXTERNAL_DOCUMENT_ROOT` and are not copied into `GR_ROOT`.

`documents.document_kind`, `canonical_identity`, and `document_year` support the observed non-GR kinds without forcing GR number/date rules onto Acts. External documents link one-to-one through `external_document_provenance` to the existing acquisition catalogue, while append-only `external_acquisition_lifecycle_events` retains DISCOVERED, DOWNLOADED, AUTO_VERIFIED, READY_FOR_INGESTION, and INGESTED milestones. URLs, authority, adapter, verification, and identity evidence remain queryable from the linked catalogue rather than being duplicated into `documents`.

## Permanent external document archive V2

`GR_ROOT` remains the unchanged 2,715-file Maharashtra collection.
`EXTERNAL_ACQUISITION_ROOT` is controlled quarantine/evidence storage only.
`EXTERNAL_DOCUMENT_ROOT` is the persistent immutable archive for verified
official external documents.

```text
official source → quarantine → PDF/identity/provenance verification
  → controlled immutable archive path → AUTO_VERIFIED_READY
  → later separately authorized ingestion
```

The source resolver maps only the closed `LOCAL_ORIGINAL` and
`EXTERNAL_OFFICIAL` classes to configured roots; HTTP callers cannot select a
filesystem root. Catalogue and document provenance retain quarantine evidence
separately from archive root identity, relative archive path, original remote
filename, safe local filename, SHA-256, size, timestamps, authority, adapter,
and identity state. Kind directories are controlled and archive filenames are
identity-derived, Windows-safe, collision-resistant, and SHA-suffixed.

## Sovereign database backup and External Acquisition V2.1

The Podman volume is operational storage, not the sovereign backup. The
`knowledge_engine.backup` boundary invokes PostgreSQL 17 logical tools inside
the existing container, copies custom/globals/schema artifacts to configured
Windows `BACKUP_ROOT`, inventories both permanent PDF roots, and performs a
full isolated restore before declaring PASS. pgvector remains native PostgreSQL
table data; vectors are not exported to CSV. See
`docs/DATABASE_BACKUP_AND_RECOVERY.md`.

V2.1 adds structured legal-title quality gates, exact title-suffix/year
canonicalization, explicit principal/amendment/consolidated semantics, and a
reusable official Divyang Sahayak date-index adapter. Parser prose and amendment
footnotes are rejected before discovery. Existing archived India Code documents
close truncated target aliases only through matching canonical identity,
existing acquisition provenance, parseable archive bytes, and exact SHA; they
never enter a new-ingestion manifest.
