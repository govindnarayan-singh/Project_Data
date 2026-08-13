# Current state

## Migrated

- Deterministic text-quality and Marathi normalization logic
- Digital/OCR PDF extraction decisions
- Metadata extraction
- Token-aware chunking
- Hard maximum-token guarantee for newly generated chunks
- Deterministic GR-number, GR-date, and subject validation
- Manifest-based, resumable, transaction-safe batch ingestion
- BAAI/bge-m3 embeddings with lazy process-local reuse
- Existing PostgreSQL/pgvector storage and cosine retrieval semantics
- Dense retrieval evaluation and CLI search
- Server-rendered Django semantic evidence-search UI
- Safe document-ID-based PDF streaming

## Controlled database

- Documents: 61
- Pages: 1,022
- Chunks/embeddings: 740

The original 11-document database was expanded with the frozen
`expansion_50_v1.csv` manifest. All 50 new PDFs persisted successfully (41
`SUCCESS`, 9 `SUCCESS_WITH_WARNING`) and added 455 pages and 411 chunks. The
original document IDs and SHA-256 identities were preserved, no existing
document was reprocessed, and a repeat default-mode run skipped all 50.

## Frozen benchmark

The unchanged 29-question benchmark after the 50-document expansion is:

- Document Recall@1: 86.21%
- Document Recall@3/5/10: 100%
- Document MRR: 0.9253
- Page Recall@1: 72.41%
- Page Recall@3: 96.55%
- Page Recall@5/10: 100%
- Page MRR: 0.8477

There were no top-10 PASS-to-FAIL changes. Five questions changed rank because
of new semantic competition; four are same-department competing GRs or related
page-ranking effects. The exact pre-expansion benchmark was reproduced by
restricting dense search to the verified unchanged original document IDs.

## Known issues and boundaries

- No stored chunk exceeds 700 tokens and every stored embedding is 1,024
  dimensions. The expansion's 411 chunks range from 121 to 700 tokens.
- Metadata validation results are application-level and are not persisted in
  dedicated PostgreSQL columns. A future persistence change requires a
  reviewed schema migration.
- The expansion has 12 metadata diagnostics: seven GR-number issues, four
  filename-derived dates requiring verification, and one short subject.
- Deterministic final-text classification flags 32 of the 455 new pages for
  extraction-quality review; these flags did not cause partial persistence.
- The corpus contains the 61 controlled documents, not all 2,715 PDFs.
- There is no LLM answer generation or RAG in this phase.
- There is no knowledge-graph extraction in this phase.

Full evidence is under `outputs/corpus_expansion/expansion_50_v1/`.

## Knowledge Graph diagnostic (2026-08-13)

A read-only deterministic diagnostic scanned all 61 documents and 1,022 stored
pages. It produced 414 candidate reference mentions across 57 documents and 42
pattern families. Resolution against current metadata yielded 1 RESOLVED_EXACT,
0 RESOLVED_STRONG, 1 AMBIGUOUS, 301 NOT_IN_CORPUS, and 111 UNPARSEABLE
candidates. The 301 NOT_IN_CORPUS candidates form 254 normalized identifier
groups, which remain an unverified upper bound because some identifier
boundaries are affected by OCR/parser noise.

The known 12 metadata diagnostics were classified as seven HIGH, four MEDIUM,
and one LOW KG impact. Review of the authoritative 32 flagged expansion pages
classified 14 IMPORTANT_BAD and 18 NON_CRITICAL_BAD; none was ignorable.

A 50-candidate Codex diagnostic source-text review measured 96.00% reference
detection precision, 93.33% reference-type accuracy, 63.33% document-number
extraction accuracy, and 82.76% date extraction accuracy. Recall was not
measured. Resolution precision was 100% for a denominator of one and therefore
does not establish production resolution quality.

The diagnostic recommendation is CONDITIONAL PASS. A provenance-first V1 with
REFERENCES and ISSUED_BY is evidence-supported after identifier parsing,
metadata, weak-page, and independent human-validation prerequisites are met.
The current `gr_relations` placeholder should remain untouched and later be
replaced through a reviewed migration. No graph rows or schema changes were
made, and PostgreSQL remained at 61 documents, 1,022 pages, 740 chunks, and 0
`gr_relations` rows. See `docs/KNOWLEDGE_GRAPH_PLAN.md` and
`outputs/knowledge_graph_diagnostic/kg_diagnostic_report.md`.

## Knowledge Graph V1 implementation (2026-08-13)

The additive, candidate-first PostgreSQL schema is operational. The production
extractor processed exactly 61 documents and 1,022 stored pages, persisting 729
reference candidates and eight controlled departments. Candidate resolution is
1 RESOLVED_EXACT, 0 RESOLVED_STRONG, 1 AMBIGUOUS, 368 NOT_IN_CORPUS, 122
UNPARSEABLE, 4 NEEDS_REVIEW, and 233 SELF_REFERENCE_REJECTED.

One exact candidate is AUTO_VERIFIED and produced one provenance-linked
REFERENCES edge (`202310111859174404.pdf` to `202212051822141504.pdf`). All 61
documents have an AUTO_VERIFIED ISSUED_BY edge. Eighteen stronger-relation
signals remain candidates; none became an authoritative strong relation.

The 100-row CODEX_SOURCE_REVIEW regression set measured 98.95% detection
precision, 95.92% recall on its labelled examples, 100% reference-kind accuracy,
98.67% document-number accuracy, and 100% date accuracy. This is an engineering
regression set, not independent human validation.

An unchanged second run inserted zero candidates, edges, and evidence links;
counts remained 729 candidates and 62 relations with no duplicates. Corpus
integrity remains 61 documents, 1,022 pages, 740 chunks/embeddings, no chunks
over 700 tokens, and 1,024 dimensions. The document identity fingerprint is
unchanged. The frozen 29-question dense benchmark reproduced all prior metrics
exactly.

`/documents/<id>/` now displays verified outgoing/incoming references,
unresolved candidates, ISSUED_BY, candidate administrative signals, evidence,
and PDF links. Graph-aware ranking and retrieval are not implemented. Full
evidence is under `outputs/knowledge_graph_v1/`.

## Administrative portal UI (2026-08-13)

The Django interface is now a production-oriented Government Administrative AI
Copilot with shared responsive navigation and a collapsible context panel.
Workspaces cover evidence search, a paginated document library, tabbed document
evidence/PDF/relationship review, graph overview, aggregated unresolved targets,
corpus health, and stored evaluation results.

The portal generates no answer, changes no ranking, and loads BGE-M3 only for a
valid submitted search. Other pages use bounded read-only PostgreSQL projections
or fixed artifacts. Verified graph edges and unresolved candidates remain
visibly distinct, and evidence retains document/page provenance. See
`docs/UI_ARCHITECTURE.md`.

## Graph development automation and local acquisition (2026-08-13)

`KG_REVIEW_MODE=strict|auto` is implemented through a central policy. Existing
REFERENCES validation remains deterministic. Auto mode can represent a safe
strong relation as AUTO_VERIFIED without falsifying HUMAN_CONFIRMED, while
strict mode requires actual human confirmation. A backed-up additive constraint
migration was applied. The 18 current signals produced zero safe promotions;
two apply passes inserted zero relations and evidence links.

The graph-guided acquisition run inventoried 2,715 PDFs, identified 61 as
already ingested, and digitally inspected at most two pages of each remaining
PDF. No OCR or embeddings ran. The 368 NOT_IN_CORPUS mentions consolidated to
233 number/kind groups. Results were 4 MATCH_EXACT, 3 MATCH_STRONG, 3 AMBIGUOUS,
212 NOT_FOUND, and 11 BLOCKED_QUALITY.

`data/manifests/graph_guided_expansion_v1.csv` contains seven selected PDFs
(four Tier A, three Tier B), SHA-256
`c0673cf3394c0dc107bb10865f4a806733be5f97dce8805bf77a63d1662f8f14`.
It contains no ingested path, duplicate path, or duplicate SHA. It potentially
covers seven groups, nine mentions, and seven citing documents, but was not
ingested. The target count was not forced because only seven identities passed.

The database remains 61 documents, 1,022 pages, 740 chunks/embeddings, 729 KG
candidates, and 62 relations. Document graph coverage is 2/61; reference
resolution is 1/374 using the documented target-bearing denominator. The frozen
29-question retrieval benchmark remains exactly unchanged. Full evidence is in
`outputs/graph_guided_expansion_v1/`.
