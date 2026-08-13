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

Next measured stages:

1. Harden acquisition resolution for the three ambiguous and 212 unmatched groups
2. Controlled ingestion of a sufficiently strong frozen graph-guided manifest
3. Repeat versioned KG extraction and evaluation
4. Independent human review when available
5. Human-confirmed AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS relations
6. Graph-aware retrieval experiment
7. Hybrid retrieval
8. Reranking
9. RAG

Do not begin a later stage without its own baseline, evaluation labels, and
explicit authorization. Dense retrieval remains unchanged in KG V1.
