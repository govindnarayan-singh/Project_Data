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

- Documents: 11
- Pages: 567
- Chunks: 329

The same 10 frozen evaluation documents were regenerated with the hardened
chunker and BGE-M3 embeddings. The eleventh earlier test document was left
unchanged. Document IDs were preserved.

## Frozen benchmark

The 29-question benchmark after controlled regeneration is:

- Document Recall@1: 100%
- Document Recall@3/5/10: 100%
- Document MRR: 1.0000
- Page Recall@1: 86.21%
- Page Recall@3: 100%
- Page Recall@5: 100%
- Page Recall@10: 100%
- Page MRR: 0.9310

## Known issues and boundaries

- No stored chunk exceeds 700 tokens. The affected Finance document now has 45
  stored chunks with a maximum of 700 tokens.
- Metadata validation results are application-level and are not persisted in
  dedicated PostgreSQL columns. A future persistence change requires a
  reviewed schema migration.
- Some populated metadata fields remain semantically suspicious; validation
  now flags them rather than rewriting them.
- The corpus contains only the controlled documents, not all 2,715 PDFs.
- There is no LLM answer generation or RAG in this phase.
- There is no knowledge-graph extraction in this phase.
