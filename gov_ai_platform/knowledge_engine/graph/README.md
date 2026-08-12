# Knowledge Graph diagnostics

Production graph extraction remains intentionally out of scope. The isolated
`diagnostics` package contains deterministic, read-only normalization,
reference-candidate extraction, and resolution utilities used for the
61-document corpus study. It is not integrated with ingestion, retrieval, the
Django application, or PostgreSQL writes.

Corpus evidence supports a small V1 based on a typed
`AdministrativeDocument`, `Department`, and the relations REFERENCES and
ISSUED_BY. Stronger semantic relations remain human-confirmed future work.

The current `gr_relations` table was not changed. The diagnostic recommends a
candidate-first, provenance-complete replacement through a reviewed migration
before production rows are created. See `docs/KNOWLEDGE_GRAPH_PLAN.md` and
`outputs/knowledge_graph_diagnostic/kg_diagnostic_report.md`.
