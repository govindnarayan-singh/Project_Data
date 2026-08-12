# Knowledge Graph plan

## Implemented V1 status

Knowledge Graph V1 is operational in PostgreSQL as a provenance-first,
candidate-first subsystem. It processes the controlled 61 documents and 1,022
stored pages without changing ingestion, embeddings, dense ranking, or the
legacy `gr_relations` table.

The production run persisted 729 candidates: 1 RESOLVED_EXACT, 0
RESOLVED_STRONG, 1 AMBIGUOUS, 368 NOT_IN_CORPUS, 122 UNPARSEABLE, 4
NEEDS_REVIEW, and 233 SELF_REFERENCE_REJECTED. One exact candidate passed all
gates and created an AUTO_VERIFIED REFERENCES edge. All 61 documents have an
AUTO_VERIFIED ISSUED_BY edge to one of eight controlled departments.

The second unchanged-corpus run inserted no candidate, edge, or evidence link.
Candidate and relation counts remained 729 and 62. Frozen retrieval metrics
were identical before and after.

## Quality and parser gates

`data/knowledge_graph/kg_quality_overrides.csv` preserves the seven high-impact
GR-number diagnostics and 14 important weak-page decisions without overwriting
authoritative metadata. Suspicious/missing target identifiers cannot resolve;
candidates sourced from flagged pages cannot become AUTO_VERIFIED.

The parser regression set contains 100 examples labelled
`CODEX_SOURCE_REVIEW`, not independent human ground truth. Measured results:

- detection precision: 98.95%;
- detection recall on the labelled examples: 95.92%;
- reference-kind accuracy: 100% of 94 assessable examples;
- document-number accuracy: 98.67% of 75 assessable examples;
- date accuracy: 100% of 71 assessable examples.

The engineering thresholds passed, enabling strict deterministic
AUTO_VERIFIED REFERENCES. Independent domain review remains the next gate.

## V1 model

Core entities are a typed AdministrativeDocument (the existing `documents`
row plus reference kind) and Department (`kg_departments`). Observed kinds are
Government Resolution, circular/Government Circular, notification, Act, Rule,
Government/department letter, Government Order, court order, scheme/policy
document, meeting minutes, corrigendum, supplement, and unknown.

Core relations are:

- REFERENCES: explicit source evidence plus a unique deterministic document
  identity match. Ambiguous, missing, malformed, weak-page, and suspicious
  target cases stay candidates.
- ISSUED_BY: existing controlled corpus department metadata mapped through a
  canonical department/alias table.

AMENDS, SUPERSEDES, PARTIALLY_SUPERSEDES, CANCELS, CORRECTS, and EXTENDS remain
candidate signals only. The production run stored 18 such candidates (9
AMENDS, 5 CORRECTS, 2 SUPERSEDES, 2 EXTENDS) and created no authoritative
strong relation. Human confirmation is mandatory.

## Provenance and lifecycle

Every reference candidate preserves source document/page, raw evidence, raw
and normalized identifier/date/department, pattern and parser version, parser
status/notes, evidence hash, stable fingerprint, resolution status/method and
evidence, confidence category, quality blocks, validation state, run IDs, and
timestamps. Raw data is never replaced by normalized data.

Validation states are CANDIDATE, AUTO_VERIFIED, HUMAN_CONFIRMED, REJECTED, and
UNRESOLVED. Resolution states are RESOLVED_EXACT, RESOLVED_STRONG, AMBIGUOUS,
NOT_IN_CORPUS, UNPARSEABLE, SELF_REFERENCE_REJECTED, and NEEDS_REVIEW.

## Resolution and acquisition

Resolution uses exact normalized document number, followed by date and
department corroboration/narrowing. Conflicts remain ambiguous. BGE-M3 vectors
and subject similarity are never identity evidence.

There are 253 normalized number/kind/date NOT_IN_CORPUS target groups after V1
parsing (233 unique number/kind groups when date variants are consolidated).
`outputs/knowledge_graph_v1/unresolved_reference_priority.csv` ranks them by
distinct citing documents, mentions, identifier completeness, date,
department, and source-page quality. No missing PDF was ingested.

## Query and UI

`GraphService` provides outgoing, incoming, unresolved, ISSUED_BY, evidence,
and summary queries without exposing SQL to Django. `/documents/<id>/` displays
verified edges, unresolved candidates, candidate relation signals, source-page
provenance, and a safe document-ID PDF link. Search cards link to this page;
dense ranking is unchanged.

## Next stages

1. Independent domain-expert review of `review_queue.csv` and `quality_sample.csv`.
2. Parser/resolver tuning from confirmed feedback.
3. Graph-guided acquisition of reviewed high-priority missing references.
4. Corpus expansion toward roughly 200 controlled documents.
5. Repeat versioned KG extraction and evaluation.
6. Introduce individually reviewed HUMAN_CONFIRMED strong relations.
7. Measure graph-aware retrieval as a separate experiment.

Hybrid retrieval, reranking, RAG, and graph-aware search are not implemented.
