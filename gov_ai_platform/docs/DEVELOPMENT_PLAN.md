# Development plan

Completed foundations:

1. Deterministic extraction, metadata validation, token-bounded chunking, and BGE-M3 embeddings
2. Manifest ingestion and controlled 61-document corpus
3. Dense retrieval evaluation and Django evidence UI
4. Knowledge Graph diagnostic
5. Candidate-first PostgreSQL Knowledge Graph V1
6. Deterministic reference parsing/resolution, review artifacts, idempotence, and graph detail UI
7. Production-oriented administrative portal UI with document, graph, corpus, and evaluation workspaces
8. Configurable automatic KG validation policy and graph-guided local-corpus acquisition V1
9. Persistent 2,715-PDF local identity catalogue and deterministic resolver V2

Next measured stages:

1. Expand corpus from authoritative external sources for 203 NOT_FOUND_LOCAL groups
2. Re-run identity indexing and deterministic resolution for acquired files
3. Consider controlled ingestion only after a sufficiently strong manifest exists
4. Repeat versioned KG extraction and evaluation
5. Independent human review when available
6. Human-confirmed AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS relations
7. Graph-aware retrieval experiment
8. Hybrid retrieval, reranking, then RAG as separately gated stages

Do not begin a later stage without its own baseline, evaluation labels, and
explicit authorization. Dense retrieval remains unchanged in KG V1.
