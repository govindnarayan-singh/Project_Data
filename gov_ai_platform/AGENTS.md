# Working agreements

- Preserve the original GR PDFs; never modify corpus files.
- Do not ingest the full 2,715-document corpus without explicit instruction.
- Reuse the current `gr-postgres` PostgreSQL container and avoid unnecessary infrastructure.
- Do not change the BAAI/bge-m3 embedding model or its 1,024 dimensions without approval.
- Preserve page-level traceability from every chunk to its source PDF.
- Never silently change retrieval-evaluation ground truth.
- Run relevant tests after modifications.
- Prefer deterministic metadata and reference parsing where practical.
- Do not introduce LLMs into extraction logic.
- Explain proposed schema changes before applying them.
- Keep web concerns separate from the knowledge engine.
