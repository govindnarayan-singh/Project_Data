# Knowledge Graph V1 schema

## Processing architecture

```text
documents + final pages
  → ReferenceParser V1
  → kg_reference_candidates
  → ReferenceResolver V1 + quality policy
  → validated kg_relations
  → GraphService
  → Django document detail
```

PostgreSQL is the sole system of record. `gr_relations` is retained as a
deprecated, zero-row compatibility placeholder; migration
`deployment/postgres/migrations/001_kg_v1.sql` is additive and does not drop it.

## Tables

### `kg_departments` and `kg_department_aliases`

Departments have unique canonical and normalized names. Aliases have a unique
normalized key, validation state, and provenance source. V1 seeds eight
departments from controlled folder metadata and creates no spelling duplicates.

### `kg_extraction_runs`

UUID run identity, start/finish timestamps, document/page/candidate/resolution/
edge counts, parser/normalizer/resolver versions, status, and notes make each
run reproducible. Failed runs are explicitly recorded.

### `kg_reference_candidates`

This is the candidate and acquisition queue. It stores:

- composite source document/page FK;
- canonical reference kind;
- raw evidence and raw/normalized identifier, date, and department;
- pattern, parser version/status/notes, relation signal;
- resolution status, nullable target FK, method/evidence/count, confidence;
- validation status and page/target-quality blocks;
- reviewer fields;
- evidence hash, unique deterministic fingerprint, first/last run IDs;
- created/updated timestamps.

Checks enforce valid enumerations, positive pages, nonempty evidence, and a
resolved target exactly when status is RESOLVED_EXACT/RESOLVED_STRONG. Indexes
cover source page, resolved target, resolution/validation state, unresolved
normalized number, and candidate relation signal.

Fingerprint identity is source document + page + reference kind + raw evidence
hash. This remains stable when identifier/date cleanup improves while preventing
duplicate evidence candidates on unchanged runs.

### `kg_relations` and `kg_relation_evidence`

Validated edges only. A typed check requires ISSUED_BY to target a department
and other relations to target a document. REFERENCES and ISSUED_BY may be
AUTO_VERIFIED. Following migration 002, a strong relation may also be
AUTO_VERIFIED only with a source candidate; otherwise it must be
HUMAN_CONFIRMED. Partial unique indexes prevent duplicate document-target or
department-target edges.

Relation evidence links one or more candidate records to an edge and has a
unique relation/candidate pair. No REFERENCES edge may be treated as valid
without this provenance.

### `kg_reviews`

Append-only review events record prior state, decision, reviewer, note, and
timestamp. The reviewer CSV leaves decision and note blank for external review.

## Normalization

Production helpers normalize NFC Unicode, zero-width characters, Devanagari
digits, whitespace, observed dash/slash/dot variants, `प्र.क्र.` and `C.R.`,
department aliases, Marathi/English/numeric dates, and observed Act/Rule titles.
Raw values always remain separately available.

Observed missing/inserted separators before series years canonicalize to one
hyphen, allowing `अभियान 2022/...` and `अभियान-2022/...` to match. Acts and
Rules use title/year keys and are not treated as GR numbers.

## Parser, resolver, and validation

The parser performs boundary detection, kind detection, identifier cleanup,
date extraction, issuer normalization, self-reference detection, relation
signal detection, and validation. It never resolves a target.

The resolver indexes only metadata allowed by the quality policy. Exact number
matches are corroborated by date/department; conflicting or duplicate matches
remain ambiguous. AUTO_VERIFIED requires a passed parser engineering gate,
VALID parser output, a unique exact/strong target, no date conflict, an allowed
source page, and target metadata allowed for resolution.

Unresolved candidates are first-class rows. Migration
`002_kg_auto_review_policy.sql` changes only the strong-relation validation
constraint: a strong edge may be `AUTO_VERIFIED` when it has a non-null source
candidate, or `HUMAN_CONFIRMED` after actual review. Application policy still
requires explicit wording, unique deterministic identity, passed quality gates,
and transactionally inserted `kg_relation_evidence`. The current graph has zero
machine-verified strong edges.

## Service and security

`knowledge_engine.graph.service.GraphService` exposes typed read-only query
operations. Django accepts only integer document identities; graph views accept
no SQL or filesystem path. OCR/PDF evidence is auto-escaped by Django templates.
PDF streaming retains the existing root-constrained document-ID endpoint.

## Future graph-aware retrieval

Dense retrieval and graph traversal remain separate. A future measured phase
may expand retrieved documents through validated edges while retaining edge
evidence. No vector score creates or validates an edge, and V1 does not change
Top-K ranking or semantics.

## Local corpus identity schema (migration 003)

`local_document_catalog` is a persistent identity index for every local PDF,
including the 61 ingested files. It is deliberately not `documents`: it has no
pages, chunks, vectors, or search visibility. Unique relative path, SHA-256,
file size/mtime, catalogue version, and upsert semantics distinguish new,
unchanged, and changed files. Indexed keys cover number, number/date,
number/department, number/date/department, legal title/year, and kind.

Identity states are `IDENTIFIED_EXACT`, `IDENTIFIED_PARTIAL`, `NO_IDENTIFIER`,
`AMBIGUOUS_METADATA`, `EXTRACTION_FAILED`, `NEEDS_OCR`, and `QUALITY_BLOCKED`.
Raw number/date/department/subject and evidence page/method coexist with every
normalized value. Acts and Rules use observed title/year canonical keys.

Migration `003_local_corpus_identity_v1.sql` additively extends candidates with
`resolved_target_catalog_id`, local resolution method/evidence/count, machine
validation, ingestion eligibility, and timestamp. A consistency constraint
requires a catalogue target exactly for `RESOLVED_LOCAL_EXACT` or
`RESOLVED_LOCAL_STRONG`. No fake `documents` row is permitted. Self/page-header
identities are `SOURCE_UNPARSEABLE`, not edges.

## External acquisition schema (migration 004)

`external_acquisition_runs` records restartable run state.
`external_acquisition_candidates` is a non-search catalogue keyed idempotently
by exact target, adapter, URL, and discovered identifier. It retains raw and
normalized identity, query, authority class, discovery/identity/acquisition/
verification states, URLs, title/date/issuer evidence, hashes/file facts,
adapter evidence, priority, and timestamps.

`INGESTED`, `LOCAL_CATALOGUED`, `EXTERNAL_DISCOVERED`, `EXTERNAL_VERIFIED`, and
`NOT_FOUND` are distinct. READY_FOR_INGESTION requires an AUTO_VERIFIED acquired
path and SHA-256; discovery cannot create a fake search document.

V1.1 uses the existing additive migration without a schema change. Discovery
URL, canonical landing URL, official document URL, redirect chain, source
adapter/authority, identity evidence, parsed-PDF facts, target high-level
state, and source-disagreement classification are preserved in the existing
typed columns plus `raw_source_evidence`. The five READY rows have immutable
quarantine paths and SHA-256 values; no `documents`, `pages`, `chunks`,
embeddings, candidates, relations, or evidence links are created by external
acquisition.
## External provenance additions (migration 005)

`documents.source_origin` separates `LOCAL_ORIGINAL` from `EXTERNAL_OFFICIAL`; `(source_origin,file_path)` is the safe path identity. `document_kind`, `canonical_identity`, and `document_year` provide minimal generic administrative identity, and an external canonical identity is unique per kind.

`external_document_provenance` is a one-to-one link from an ingested document to one existing acquisition candidate and snapshots only the immutable acquisition path and SHA. `external_acquisition_lifecycle_events` retains append-only acquisition milestones. Strong-relation automatic promotion rejects an external target whose provenance link is absent. Source origin is document metadata/provenance, not a graph relation type.

## External permanent archive additions (migration 006)

The external catalogue additively stores archive root identity, relative path,
safe local filename, original remote filename, archive status, and timestamp.
`EXTERNAL_OFFICIAL_ARCHIVED` requires complete AUTO_VERIFIED archive metadata.
`external_document_provenance` stores permanent archive linkage separately
from the original quarantine path, and lifecycle evidence includes the archive
milestone. Archive state alone never implies search ingestion.

## KG Brain V2 read model (no migration)

Validated `kg_relations`, `kg_relation_evidence`, and
`kg_reference_candidates` remain the system of record. Recursive traversal
accepts only validated document edges, an explicit direction, and depth 1–5;
every hop is hydrated with its original candidate evidence. Source is always
the actor/citing document and target the affected/cited document. Cycles,
effect flags, components, priorities, and benchmarks are read-only projections
or artifacts, not legal facts or persisted relations.
