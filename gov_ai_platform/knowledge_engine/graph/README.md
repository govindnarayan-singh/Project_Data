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

Only REFERENCES and ISSUED_BY may be created automatically. AMENDS,
SUPERSEDES, PARTIALLY_SUPERSEDES, CANCELS, CORRECTS, and EXTENDS remain
candidates until human confirmation. Vector similarity and subject similarity
are never graph identity or edge evidence.

See `docs/KNOWLEDGE_GRAPH_SCHEMA.md` and `docs/KNOWLEDGE_GRAPH_PLAN.md`.
