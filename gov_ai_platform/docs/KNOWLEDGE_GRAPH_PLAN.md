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

AMENDS, SUPERSEDES, PARTIALLY_SUPERSEDES, CANCELS, CORRECTS, and EXTENDS began
as candidate signals only. The production run stored 18 such candidates (9
AMENDS, 5 CORRECTS, 2 SUPERSEDES, 2 EXTENDS) and created no authoritative
strong relation. Strict mode requires human confirmation; the later automatic
development policy below permits AUTO_VERIFIED only behind stronger gates.

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

## Automatic development review policy

`KG_REVIEW_MODE` is parsed by `KnowledgeGraphValidationPolicy` and accepts only
`strict` or `auto` (default). `AUTO_VERIFIED` means deterministic machine
validation and never implies human review. `HUMAN_CONFIRMED` remains reserved
for an actual human decision.

Both modes retain the existing REFERENCES gates: VALID parser output,
structurally valid identifier, unique exact/strong resolution, no date
conflict, no source-page or target-metadata block, self-reference rejection,
and preserved evidence. Strict mode requires HUMAN_CONFIRMED for strong
relations. Auto mode may use AUTO_VERIFIED for a strong relation only when an
approved HIGH_CONFIDENCE_EXPLICIT phrase also links the action to that specific,
uniquely resolved target. Vectors and subject similarity are forbidden.

The phrase catalogue separates:

- HIGH_CONFIDENCE_EXPLICIT: wording such as “is hereby amended,” “corrigendum
  to,” “is hereby superseded,” or Marathi equivalents that explicitly perform
  the action;
- CONTEXT_DEPENDENT: generic occurrences such as amendment/corrigendum in a
  title, “extended,” or a relation word without an explicit target link;
- AMBIGUOUS: no approved phrase family.

All 18 current signals were dry-run. No AMENDS, CORRECTS, SUPERSEDES, EXTENDS,
CANCELS, or PARTIALLY_SUPERSEDES candidate passed all gates. Applying the policy
twice inserted zero relations and zero evidence links. No HUMAN_CONFIRMED state
was created.

## Graph-guided acquisition V1

```text
NOT_IN_CORPUS candidates
  → conservative normalized number/kind consolidation
  → safe local PDF inventory
  → at-most-two-page digital metadata index (no OCR)
  → exact deterministic identifier matching
  → transparent priority score
  → frozen manifest for future controlled ingestion
```

The inventory records relative path, folder department, bytes, page count,
filename date, SHA-256, and ingested state. The resolver accepts identity
evidence only; semantic similarity is never used. Exact normalized identifiers
are corroborated where possible with a text date or folder department.
Conflicts and multiple distinct SHA identities remain AMBIGUOUS.

Priority score weights are additive and visible:

- 100 per distinct citing document;
- 10 per mention, capped at 25;
- 120 for a stored strong-relation signal;
- 80 for a complete identifier (20 otherwise);
- 30 for a reference date and 20 for department evidence;
- 3 per unblocked mention, capped at 10;
- 400 for MATCH_EXACT, 250 for MATCH_STRONG, and 50 for AMBIGUOUS.

The run inventoried all 2,715 PDFs and excluded the 61 ingested identities.
Because filenames are opaque timestamps, the remaining 2,654 PDFs formed the
cheap candidate identity index. At most two digital pages were inspected (5,258
pages total); OCR, chunking, embeddings, and ingestion were not run. Of 233
consolidated groups, 4 were MATCH_EXACT, 3 MATCH_STRONG, 3 AMBIGUOUS, 212
NOT_FOUND, and 11 BLOCKED_QUALITY.

The frozen manifest contains seven unique PDFs (four Tier A, three Tier B), not
the aspirational 100–140. The count was not padded with random documents. It is
safe but too small to use as the intended expansion batch; acquisition
resolution should be hardened before controlled ingestion.

## Coverage and resolution metrics

`graph_document_coverage` is unique documents participating in at least one
document-to-document relation divided by total ingested documents. ISSUED_BY is
excluded. Current coverage is 2/61 (3.2787%). Department relation coverage is
reported separately and is 61/61 (100%).

Reference resolution rate is:

```text
(RESOLVED_EXACT + RESOLVED_STRONG)
--------------------------------------------------------------
resolved + AMBIGUOUS + NOT_IN_CORPUS + NEEDS_REVIEW
```

SELF_REFERENCE_REJECTED is not a target and UNPARSEABLE lacks a defensible
identity, so both are excluded. The current rate is 1/374 (0.2674%).

## Local Corpus Identity V1

The hardened acquisition stage now maintains a PostgreSQL identity catalogue
for all 2,715 local PDFs. Version 1.0.2 used 7,227 bounded digital page probes
and selectively OCRed 43 pages across 31 PDFs with Tesseract `mar+eng`. Final
identity status is 1,062 exact and 1,653 partial. It extracted 2,037 unique
normalized identifiers, 626 text dates, all 2,715 folder departments, and 804
deterministic document kinds. The 314 duplicate identifier groups were not
merged.

Resolver V2 evaluated 233 groups: 2 RESOLVED_LOCAL_EXACT, 3
RESOLVED_LOCAL_STRONG, 6 AMBIGUOUS_LOCAL, 203 NOT_FOUND_LOCAL, 11
BLOCKED_QUALITY, and 8 SOURCE_UNPARSEABLE self/page-header groups. Exact full
keys are preferred. Structural series/year/proposal keys require independent
date or explicit-department corroboration. Filename dates corroborate but are
never publication proof. No fuzzy edit, subject similarity, or vector identity
proof is used.

The five eligible targets are frozen in
`data/manifests/kg_resolved_local_targets_v1.csv` (two Tier B, three Tier C),
with no ingested/duplicate/missing/outside-root/hash-conflict row. None of the
18 strong relation signals gained a resolved local target; HUMAN_CONFIRMED is
zero.

The measured outcome supports **C. EXPAND CORPUS FROM OTHER SOURCES**: 109
groups appear truly absent, 65 are unstored Acts/Rules, 24 are external/non-GR,
and five are damaged/incomplete; six remain locally ambiguous. Do not treat a
five-row batch as graph completion.
