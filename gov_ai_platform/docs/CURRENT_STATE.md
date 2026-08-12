# Current state

## Migrated

- Deterministic text-quality and Marathi normalization logic
- Digital/OCR PDF extraction decisions
- Metadata extraction
- Token-aware chunking
- Hard maximum-token guarantee for newly generated chunks
- Deterministic GR-number, GR-date, and subject validation
- BAAI/bge-m3 embeddings with lazy process-local reuse
- Existing PostgreSQL/pgvector storage and cosine retrieval semantics
- Dense retrieval evaluation and CLI search
- Server-rendered Django semantic evidence-search UI
- Safe document-ID-based PDF streaming

## Controlled database

- Documents: 11
- Pages: 567
- Chunks: 347

The database is the existing controlled corpus and has not been re-ingested.

## Frozen benchmark

The 29-question, 10-GR benchmark baseline is:

- Document Recall@1: 100%
- Page Recall@5: 96.55%
- Page Recall@10: 100%

## Known issues and boundaries

- Four historical Finance chunks in the controlled database still exceed the
  configured 700-token ceiling. The fixed chunker produces 45 chunks with a
  maximum of 700 for that document in memory, but the stored rows and
  embeddings were intentionally not replaced in this phase.
- Metadata validation results are application-level and are not persisted in
  dedicated PostgreSQL columns. A future persistence change requires a
  reviewed schema migration.
- Some populated metadata fields remain semantically suspicious; validation
  now flags them rather than rewriting them.
- The corpus contains only the controlled documents, not all 2,715 PDFs.
- There is no LLM answer generation or RAG in this phase.
- There is no knowledge-graph extraction in this phase.
