# Development plan

Completed foundations:

1. Deterministic extraction, metadata validation, token-bounded chunking, and BGE-M3 embeddings
2. Manifest ingestion and controlled 61-document corpus
3. Dense retrieval evaluation and Django evidence UI
4. Knowledge Graph diagnostic
5. Candidate-first PostgreSQL Knowledge Graph V1
6. Deterministic reference parsing/resolution, review artifacts, idempotence, and graph detail UI
7. Production-oriented administrative portal UI with document, graph, corpus, and evaluation workspaces

Next measured stages:

1. Independent human review of the KG sample and review queue
2. Parser and resolution tuning from human feedback
3. Graph-guided ingestion of reviewed high-priority missing references
4. Controlled corpus expansion toward approximately 200 documents
5. Repeat versioned KG extraction and evaluation
6. Human-confirmed AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS relations
7. Graph-aware retrieval experiment
8. Hybrid retrieval
9. Reranking
10. RAG

Do not begin a later stage without its own baseline, evaluation labels, and
explicit authorization. Dense retrieval remains unchanged in KG V1.
