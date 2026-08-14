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
10. External Acquisition V1.1 official registry, security boundary, live India Code adapter, five-file verified manifest, and idempotent catalogue

Next measured stages:

1. Ingest the five AUTO_VERIFIED external Acts only through a separately authorized frozen production batch
2. Harden documented Maharashtra GR/e-Gazette adapters without bypassing CAPTCHA or stateful forms
3. Preserve the Rights Act version conflict for explicit version selection/review
4. Expand the independent KG relationship benchmark
5. Independent human review when available
6. Human-confirmed AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS relations
7. Graph-aware retrieval only after closure is materially better

Do not begin a later stage without its own baseline, evaluation labels, and
explicit authorization. Dense retrieval remains unchanged in KG V1.
## Completed: external ingestion and graph closure V1

The five verified official Acts are now production documents with origin-aware safe storage, kind-aware metadata, BGE-M3 embeddings, acquisition lineage, lifecycle history, and AUTO_VERIFIED KG closure. Ingestion and KG replay are idempotent, and all integrity gates pass.

Next recommended action: run the planned next external acquisition batch from `outputs/external_ingestion_v1/next_external_target_priority.csv`. This task produced the 30-target plan only; it did not execute another live acquisition. Dense retrieval changes remain measurement evidence for the later hybrid/reranking phase and were not tuned here.

## Completed: permanent archive and External Acquisition V2

1. Added configured `EXTERNAL_DOCUMENT_ROOT` and closed multi-root resolution.
2. Backfilled all five ingested Acts with byte equality, SHA, provenance, and
   lifecycle verification; no reingestion or embeddings ran.
3. Executed and cache-replayed the exact frozen 30-target batch.
4. Preserved CAPTCHA blocks and official version ambiguity without inferred or
   HUMAN_CONFIRMED decisions.

Next measured stage: harden reusable official-source coverage and upstream
parser target quality. Do not ingest the empty V2 manifest or collapse legal
versions without explicit version evidence.

## Completed: sovereign backup V1 and acquisition hardening V2.1

1. Created and full-restore-verified a PostgreSQL 17 custom dump on Windows D:
   with native pgvector data, globals/schema dumps, checksums, and source inventories.
2. Replayed exactly the frozen 30-target batch with structured legal target
   quality, title-suffix/year resolution, and version semantics.
3. Added a reusable official Divyang Sahayak public-index adapter without
   bypassing CAPTCHA; runtime DNS/source access remains a measured blocker.
4. Kept the empty V2.1 manifest out of ingestion and preserved all search,
   graph, archive, and retrieval baselines.

Next measured stage: expand official source adapters further. Do not expand the
target batch until alternate government-domain coverage is executable and
identity-complete.

## Completed: native database runtime and local-first acquisition V2.2

1. Restored the verified database into native Windows PostgreSQL 17.11 with
   pgvector 0.8.6 and switched Django to `127.0.0.1:5434`; Podman is rollback-only.
2. Created and isolated-restore-tested a new native logical snapshot with exact
   71-document/1,237-vector integrity.
3. Changed external acquisition to Incoming-first local verification and
   immutable `External_Official` promotion.
4. Replayed the same frozen 30 targets with five additional issuer-scoped
   Maharashtra department sources and strict identity/version rules.
5. Produced a one-row V2.2 manifest for the archived National Green Tribunal
   Act. No ingestion, embeddings, or KG changes were run.

Next measured stage: review the V2.2 manifest under a separately authorized
ingestion task and continue source-specific work for the remaining unresolved
administrative targets. Do not loosen identity rules or bypass CAPTCHA.
