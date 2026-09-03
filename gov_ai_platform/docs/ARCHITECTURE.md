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
deterministic. Authorized network bytes go first to configured
`EXTERNAL_INCOMING_ROOT` and must pass HTTPS/domain, redirect, timeout,
size, content-type, PDF-signature, filename, hash, and identity gates. See
`docs/EXTERNAL_ACQUISITION.md`.

External Acquisition V1.1 proves the live legal-document path:

```text
Frozen unresolved target
  → document-kind strategy
  → configured official adapter
  → minimal public identity query
  → official result and canonical landing page
  → official PDF bytes stored in local Incoming
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
`EXTERNAL_INCOMING_ROOT` is controlled staging/evidence storage only.
`EXTERNAL_DOCUMENT_ROOT` is the persistent immutable archive for verified
official external documents.

```text
official source → Incoming → local PDF/identity/provenance verification
  → controlled immutable archive path → AUTO_VERIFIED_READY
  → later separately authorized ingestion
```

The source resolver maps only the closed `LOCAL_ORIGINAL` and
`EXTERNAL_OFFICIAL` classes to configured roots; HTTP callers cannot select a
filesystem root. Catalogue and document provenance retain Incoming evidence
separately from archive root identity, relative archive path, original remote
filename, safe local filename, SHA-256, size, timestamps, authority, adapter,
and identity state. Kind directories are controlled and archive filenames are
identity-derived, Windows-safe, collision-resistant, and SHA-suffixed.

## Native database runtime and External Acquisition V2.2

Native Windows PostgreSQL 17.11 with pgvector 0.8.6 on `127.0.0.1:5434` is the
normal database runtime. Podman is rollback-only. The `knowledge_engine.backup`
boundary invokes the configured native PostgreSQL logical tools, writes
custom/globals/schema artifacts to Windows `BACKUP_ROOT`, inventories both
permanent PDF roots, and performs a full isolated restore before declaring
PASS. Vectors remain native PostgreSQL table data and are never exported to
CSV. See
`docs/DATABASE_BACKUP_AND_RECOVERY.md`.

V2.1 adds structured legal-title quality gates, exact title-suffix/year
canonicalization, explicit principal/amendment/consolidated semantics, and a
reusable official Divyang Sahayak date-index adapter. Parser prose and amendment
footnotes are rejected before discovery. Existing archived India Code documents
close truncated target aliases only through matching canonical identity,
existing acquisition provenance, parseable archive bytes, and exact SHA; they
never enter a new-ingestion manifest.

V2.2 keeps the exact frozen 30 targets and adds issuer-scoped Maharashtra
department adapters. Every response body is atomically staged in Incoming;
PDF parsing, identity checks, SHA calculation, and archive promotion then read
only local bytes. A manifest can reference only immutable `EXTERNAL_OFFICIAL`
paths. Network content is never used as the post-staging document of record.

## KG Brain V2

The graph remains in native PostgreSQL. Bounded recursive CTEs support
upstream, downstream, mixed-history, and shortest directed paths at default
depth 3 and maximum depth 5. Every hop retains relation ID, validation state,
candidate/evidence links, source page, raw excerpt, depth, and path position;
visited-node arrays terminate and report cycles.

Relation direction is actor to affected target: `A AMENDS B` means A performs
the action on B. Automatic promotion creates `AUTO_VERIFIED`, never
`HUMAN_CONFIRMED`, only after all deterministic gates. Effect flags are
read-only graph facts, not legal status. Current measurements do not justify
Neo4j.
## KG-controlled acquisition boundary

The KG completion queue controls ordering, but not trust. Network discovery
terminates at a durable Incoming write. A separate local verification boundary
reopens those bytes and applies PDF security, deterministic identity/version,
SHA, provenance, and three-way duplicate checks (search corpus, local GR
catalogue, external archive). Promotion copies the exact verified Incoming
bytes into the immutable External_Official root; future ingestion can consume
only that permanent root.

Acquisition catalogue writes are bounded and idempotent and are separate from
the search corpus and KG. Normal database runtime is native Windows PostgreSQL
17 on `127.0.0.1:5434`; Podman is not a runtime fallback. Routine task backups
are disabled, and an unexpected destructive schema requirement is a stop
condition rather than an automatic backup-and-proceed path.

## KG Brain V3 completion cycle

KG Brain V3 composes the frozen V2 graph-priority queue, strict local-first
official acquisition, a verified-manifest gate, deterministic KG reruns, and
read-only graph analysis. An empty verified manifest is a normal terminal gate:
ingestion and embedding are skipped while source diagnostics, relation review,
topology, closure, history, performance, and benchmark artifacts still run.
The evidence-derived V3 benchmark contains 85 distinct query cases and labels
itself `AUTO_VERIFIED_SOURCE_EVIDENCE`, never independent human ground truth.

## KG Brain V4 source-coverage boundary

V4 inserts an operational coverage decision before network discovery:

```text
frozen target
  → kind + explicit issuer + jurisdiction/year support
  → public search executable + measured network reachability
  → target queryable on an allowlisted official source
  → discovery → Incoming → local PDF/SHA/identity verification
  → External_Official only after exact verification
```

The source registry records stable coverage contracts; the matrix records
run-specific reachability and queryability. The Maharashtra Law and Judiciary
Act List is a reusable state-law adapter with `lj.maharashtra.gov.in` and
official `cdnbbsr.s3waas.gov.in` PDF delivery. Central Acts remain routed to
India Code. CAPTCHA/stateful sources remain registered official coverage but
are not executable adapters, and no bypass or private endpoint is used.

## KG Brain V6 official-source architecture

```text
frozen graph targets
  -> source-family graph value
  -> historical + operational coverage gates
  -> reusable official adapter
  -> Government_Documents/Incoming
  -> local PDF / bounded OCR / exact identity / SHA / duplicate gates
  -> Government_Documents/External_Official
  -> verified V6 manifest
  -> controlled native-PostgreSQL ingestion
  -> deterministic KG rerun and closure audit
```

Maharashtra S3WaaS sources share allowlisted public-index discovery and title,
kind, ordinal, year, and version filters. India Code uses its public DSpace 9.1
REST surface and ORIGINAL bundle rather than the retired `.nic.in`
`simple-search` HTML endpoint. Source metadata remains provisional: the local
Incoming PDF is always the identity authority. OCR is bounded and local, and
archive promotion is byte-identical and idempotent.

V6 does not alter the runtime boundary: PostgreSQL 17/pgvector is native on
Windows, no Podman fallback is invoked, no routine database backup is created,
and graph relations are deterministic database facts rather than embedding
similarity. UI, RAG, hybrid retrieval, reranking, Neo4j, and agent orchestration
remain outside this work package.

## KG Brain V7 frozen-anchor boundary

```text
78 anchor documents
  -> frozen parser-V2 first-hop mentions and exact targets
  -> local catalogue/archive closure
  -> READY frozen targets only -> official acquisition -> Incoming
  -> local identity/SHA gates -> permanent source root
  -> anchor-only manifest -> support-document ingestion
  -> KG rerun -> frozen-anchor closure evaluation

support-document references -> second-hop report only -> no V7 acquisition
```

Anchor and support semantics are artifact-level evaluation roles; no schema
field is required. All post-ingestion graph candidates remain normal database
facts, while frozen CSV membership prevents support-document references from
changing V7 success metrics. The graph-aware retrieval decision is a readiness
output only; retrieval implementation, RAG, LLMs, agents, hybrid ranking,
reranking, UI changes, and Neo4j remain outside V7.

## Graph-aware retrieval V1 boundary

Graph Retrieval V1 is a read-only experimental layer after the unchanged dense
retriever. `GraphAwareRetrievalService` obtains dense chunks first, selects
unique Top-1/3/5 seed documents, and queries PostgreSQL for exactly one hop of
validated document-to-document `REFERENCES`. Incoming, outgoing, and
bidirectional modes remain explicit. Graph SQL is outside Django views and is
not mixed into pgvector similarity SQL.

Graph additions carry complete seed, edge, direction, validation, evidence-page,
candidate, and source/target provenance. Existing dense documents become
corroborations rather than duplicates. There is no score fusion, reranking,
graph mutation, Neo4j, LLM, RAG, agent, or UI change. The default remains
`RETRIEVAL_MODE=dense`. See `docs/GRAPH_RETRIEVAL_V1.md`.

## Dynamic Knowledge Graph Explorer V2.1 boundary

The server-rendered `/knowledge-graph/` workspace queries native PostgreSQL
through `GraphExplorerRepository` and `GraphExplorerService`. Repository
transactions are explicitly read-only; Django exposes only bounded GET routes
for summary, graph, neighborhood, search, node detail, edge provenance, and
components. The graph is refreshed from current database rows and is never a
frozen browser copy or a second persistence layer.

Cytoscape.js 3.34.2 is pinned and served from local Django static assets under
the MIT license. Full graph, filtered loading, explicit truncation, and 1/2/3
hop modes support future corpus growth. Candidate relations remain excluded;
validated relation direction and full evidence provenance remain intact. See
`docs/KNOWLEDGE_GRAPH_VISUALIZATION.md`.

V2 keeps the repository and GET-only boundary, but adds an explicit graph scope
contract. `documents` returns document nodes and validated document-to-document
relations; `administrative` also materializes only departments targeted by a
displayed validated relation. Summary totals distinguish 87 corpus documents,
12 department registry rows, 11 active departments, 98 total administrative
nodes, 50 document relations, and 137 total validated edges. Each graph response
also reports the corresponding displayed counts after filters, component scope,
and response bounds. Display labels are a deterministic projection; canonical
identity and provenance fields are never changed.

Frontend state for scope, filters, panels, layout, focus/fullscreen, refresh,
and selection is centralized in the explorer module. The graph workspace owns
its filter drawer and inspector, allowing Fullscreen API presentation without
losing controls or provenance. No new persistence, graph engine, layout plugin,
remote asset, retrieval integration, or write endpoint is introduced.

V2.1 adds a frontend release boundary after a V2 regression exposed cached V1
JavaScript/CSS running against the refactored DOM. The explorer CSS and
JavaScript now use the deterministic `v=2.1.0` static query version. Startup
validates a small critical DOM contract, renders the API graph before updating
optional summary/analytics widgets, and binds optional controls independently.
The browser distinguishes loading, empty, filtered-to-zero, API failure, and
client-render failure; none is allowed to masquerade as a valid zero-node
graph. Playwright remains a development-only acceptance dependency and drives
the real server-rendered page and read-only API.
