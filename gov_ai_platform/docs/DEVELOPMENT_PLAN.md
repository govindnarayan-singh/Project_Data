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

1. Obtain explicit external-data authorization for the official 20-target batch
2. Harden official adapters from measured live behavior without bypassing access controls
3. Verify and quarantine only exact/strong official candidates
4. Ingest externally only after a non-empty AUTO_VERIFIED/READY manifest exists
5. Expand the independent KG relationship benchmark
6. Independent human review when available
7. Human-confirmed AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS relations
8. Graph-aware retrieval only after closure is materially better

Do not begin a later stage without its own baseline, evaluation labels, and
explicit authorization. Dense retrieval remains unchanged in KG V1.
