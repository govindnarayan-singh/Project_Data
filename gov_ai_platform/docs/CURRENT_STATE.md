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
