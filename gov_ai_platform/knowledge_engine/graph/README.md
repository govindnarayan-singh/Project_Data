# Knowledge Graph V1

This package implements deterministic, provenance-first Knowledge Graph V1.

- `normalization.py`: raw-preserving identifiers, dates, departments, Acts/Rules
- `parser.py`: typed reference candidates and candidate relation signals
- `resolver.py`: exact/strong deterministic identity resolution only
- `quality.py`: metadata/page override policy without source-data rewrites
- `repository.py`: additive PostgreSQL persistence and graph queries
- `pipeline.py`: versioned, idempotent 61-document extraction
- `service.py`: read-only application boundary used by Django
- `evaluation.py`: CODEX_SOURCE_REVIEW parser regression metrics
- `diagnostics/`: retained pre-production diagnostic implementation

`KG_REVIEW_MODE=strict|auto` controls a central validation policy. REFERENCES
retain their deterministic machine gate in both modes. In strict mode, strong
relations require actual `HUMAN_CONFIRMED` review. In auto mode, a strong edge
may be `AUTO_VERIFIED` only when an approved explicit phrase, structurally
valid target identifier, unique deterministic resolution, quality gates, and
candidate provenance all pass. No current V1 strong signal passes every gate.
Vector and subject similarity are never identity or edge evidence.

`knowledge_engine.acquisition` inventories the local PDF corpus, consolidates
unresolved groups, performs limited digital metadata probes, and produces a
graph-guided manifest. It never ingests, chunks, embeds, or changes documents.

See `docs/KNOWLEDGE_GRAPH_SCHEMA.md` and `docs/KNOWLEDGE_GRAPH_PLAN.md`.

`knowledge_engine.corpus_identity` is the V2 local identity layer. It stores
all 2,715 PDF identities outside the search corpus, adaptively probes at most
five digital and three OCR pages, resolves through indexed deterministic keys,
persists machine-labelled local targets, rejects self/ambiguous matches, and
writes the safe graph-completion manifest. Catalogue rows never have chunks or
embeddings.

`closure.py` implements separate mention, exact-target, local-availability,
known-availability, and document-graph coverage metrics. The sibling
`knowledge_engine.external_acquisition` package owns official-source adapters,
deterministic queries, identity gates, network/download safety, Incoming, and
the non-search external catalogue.

External Acquisition V1.1 produced five AUTO_VERIFIED_READY Act files and an
idempotent external manifest without inserting them into search. Closure uses
their exact stored KG target keys only for Known Availability Closure; it does
not treat Incoming/catalogue records as graph documents or create edges.
External official targets resolve through `documents.document_kind` plus `canonical_identity`; Acts and Rules are not forced into GR number/date semantics. Automatic strong-relation promotion additionally requires complete `external_document_provenance` when its target has `source_origin=EXTERNAL_OFFICIAL`.

Permanent external custody is separate from graph membership. An archived
catalogue target may increase known/local-archived availability but cannot
create an edge until separately authorized ingestion produces a real document
target and ordinary evidence gates pass. Quarantine never counts as permanent
archive.
