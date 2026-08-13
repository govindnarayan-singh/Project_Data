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
