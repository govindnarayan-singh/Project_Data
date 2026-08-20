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

## Local corpus identity and KG resolution V2 (2026-08-13)

Additive migration 003 created a non-search PostgreSQL identity catalogue and
local-target candidate fields. The verified pre-migration custom backup is
`outputs/backups/pre_corpus_identity_v1_20260813.dump` (6,041,050 bytes,
SHA-256 `4e23fec60526f4159b9a3031b08b4edd358cabaf12e3944233445b67f48e5b6f`).

All 2,715 PDFs are catalogued idempotently: 1,062 IDENTIFIED_EXACT and 1,653
IDENTIFIED_PARTIAL. The build used 7,227 digital probes and 43 selective OCR
pages across 31 PDFs. It produced 2,037 unique identifiers, 314 duplicate
identifier groups, 626 text dates, 2,715 departments, and 804 known kinds. No
full PDF was OCRed, chunked, embedded, or ingested.

Resolution V2 over 233 groups is 2 exact, 3 strong, 6 ambiguous, 203 not found,
11 quality blocked, and 8 source-unparseable self/page-header groups. Automatic
local resolution is 2.15%; mention coverage is 1.63%; distinct citing-document
coverage is 8.93%. The safe five-row manifest contains two Tier B and three Tier
C targets, SHA-256
`2b0a705996160ad5da54e60c5a4606a4c129405f3b3e9b48cd22418d718e2f1b`.

The earlier seven-row probe accepted two cases that fail V2 date evidence and
used source-folder department where no explicit reference department existed.
V2 does not preserve those assumptions. Five is the defensible result, not a
forced increase. All 18 strong signals remain candidates and HUMAN_CONFIRMED is
zero. Search storage remains 61 documents, 1,022 pages, and 740
chunks/embeddings with the same fingerprint. Evidence is under
`outputs/corpus_identity_v1/`; recommendation: **C. EXPAND CORPUS FROM OTHER
SOURCES**.

## KG Brain Phase V2 local closure (2026-08-14)

The frozen five-row local manifest retained SHA-256
`2b0a705996160ad5da54e60c5a4606a4c129405f3b3e9b48cd22418d718e2f1b`.
All five PDFs ingested and replay skipped all five. The corpus is now 66
documents, 1,044 pages, and 765 chunks/1,024-dimensional embeddings. A real
metadata defect was corrected: the earliest labelled header date wins across
named/numeric formats and accepts compound `:-` separators. Atomic reprocessing
preserved document IDs.

KG now has 771 candidates and 73 relations: seven REFERENCES and 66 ISSUED_BY.
All five intended targets closed as provenance-linked AUTO_VERIFIED REFERENCES;
one additional reciprocal REFERENCES edge was found. KG replay inserted zero
candidates, relations, or evidence. All 18 strong signals remain unpromoted and
HUMAN_CONFIRMED remains zero.

Closure before -> after: reference mentions 1/374 -> 8/392, referenced targets
1/238 -> 7/252, local/known availability 6/238 -> 7/252, and graph document
coverage 2/61 -> 12/66. Frozen Recall@5/@10 remains 100%; only Q006 changed
rank, with no Top-10 pass-to-fail transition.

External Acquisition V1 selected 20 of 237 eligible unresolved targets. The
environment blocked live query egress, so all 20 attempts are BLOCKED, no file
was downloaded or verified, and the external manifest is empty. Evidence is
under `outputs/kg_brain_phase_v2/`.

## External Acquisition V1.1 live result (2026-08-14)

Public official-source access is now available from the project runtime. The
frozen 20-target batch was replayed without changing membership. Thirteen
Maharashtra administrative targets are `BLOCKED_BY_SOURCE` because the official
GR search CAPTCHA is not bypassed. Seven Act targets were queried through India
Code; 21 official candidates were discovered.

Five Act targets are AUTO_VERIFIED_READY after official landing-page, PDF,
parsed-title/year, authority, hash, local staging, and provenance gates. The
Rights of Persons with Disabilities Act remains AMBIGUOUS due to original and
amended official variants, and the generic Amendment Act target is an identity
mismatch. The five-row manifest SHA-256 is
`03fbb7f0dd07b28b714cbb7ba938b0544797f74fa3f69f46d99ddeab9fa0424a`.
Replay inserted zero catalogue rows.

Known Availability Closure is now 12/252 (4.76%), up from 7/252 (2.78%).
Reference Mention Closure remains 8/392, Referenced Target and Local
Availability Closure remain 7/252, and Graph Document Coverage remains 12/66.
The search corpus remains 66 documents, 1,044 pages, and 765 chunks/embeddings;
the frozen dense benchmark is identical. Evidence is under
`outputs/external_acquisition_v1_1/`.
## External ingestion V1 complete (2026-08-14)

The searchable corpus now contains 71 documents, 1,322 pages, and 1,237 chunks/1024-D BGE-M3 embeddings. Five immutable `EXTERNAL_OFFICIAL` Acts (document IDs 70–74) were added from the frozen verified manifest; they account for 278 pages and 472 chunks, with zero OCR pages. Provenance completeness and intended external-target closure are both 5/5.

The deterministic KG now contains 1,408 candidates and 88 relations: 17 REFERENCES and 71 ISSUED_BY, with no strong or HUMAN_CONFIRMED relations. Closure is: reference mentions 22/1,016, referenced targets 12/577, local availability 12/577, known availability 12/577, and graph document coverage 23/71. The larger denominators come from references extracted from the Acts themselves.

The unchanged dense benchmark now has document Recall@1/3/5/10 of 82.76%/96.55%/96.55%/96.55% and MRR 0.8966; page Recall@1/3/5/10 is 68.97%/93.10%/96.55%/96.55% and MRR 0.8190. Q017 is the only changed question and is classified as new semantic/page-level competition. Retrieval was measured, not tuned.

## External Acquisition V2 and local sovereignty (2026-08-14)

`EXTERNAL_DOCUMENT_ROOT` now contains byte-identical permanent copies of all
five ingested external Acts: 3,174,841 bytes with zero missing files, SHA
mismatches, duplicate hashes, path collisions, or orphans. Quarantine remains
evidence storage and is not the authoritative production source.

The exact 30-row plan was frozen at SHA-256
`8d14c5a80c6cc618cf73f64493f80fac6457ee3bec27aad222b58e7a9d2725a8`
and executed live. India Code returned 17 official candidates for eight Act
targets. Two Rights Act target groups remain version-ambiguous, six fragmentary
Act targets mismatch, and six were not found authoritatively. All 16
Maharashtra administrative targets remain CAPTCHA-blocked; controls were not
bypassed. No V2 target reached final AUTO_VERIFIED_READY, so the V2 manifest is
empty and the search corpus, KG, embeddings, and dense metrics are unchanged.
Cache replay inserted zero catalogue rows and reproduced the manifest SHA.

## Sovereign Backup V1 and External Acquisition V2.1 (2026-08-14)

A 10,755,917-byte PostgreSQL custom dump is stored under configured Windows D:
backup storage with SHA-256
`ce3f7cbea14263c5598d1865c3206427612bae8a193e88b1e7da5118eafba76d`.
Archive inspection found pgvector and all required tables/data. A full isolated
restore reproduced 71/1,322/1,237/1,237 search counts, 1,024 vector dimensions,
1,408 KG candidates, 88 relations, 17 REFERENCES, 71 ISSUED_BY, 2,715 local
catalogue rows, and 63 external catalogue rows. Inventories cover 2,715 GR PDFs
and five external PDFs with zero SHA mismatches. Production was unchanged and
the temporary database was removed.

V2.1 replayed the same 30-row SHA
`8d14c5a80c6cc618cf73f64493f80fac6457ee3bec27aad222b58e7a9d2725a8`.
Eight false legal targets are rejected as identifier-extraction fragments.
Three truncated title/year aliases resolve exactly to already-ingested India
Code documents through existing provenance and archive SHA; two Rights Act
groups remain `AMBIGUOUS_VERSION`. The National Green Tribunal landing fetch
remained unavailable. The runtime could not reach the new official Divyang
Sahayak domain, so all 16 CAPTCHA targets remain source-blocked and no CAPTCHA
was bypassed. No new document reached AUTO_VERIFIED_READY; the V2.1 manifest is
empty. Search/KG counts and dense retrieval remain unchanged.

## Native PostgreSQL and External Acquisition V2.2 (2026-08-14)

The application now uses native Windows PostgreSQL 17.11 plus pgvector 0.8.6
at `127.0.0.1:5434`; Podman is rollback-only. The verified sovereign dump was
restored with exact counts of 71 documents, 1,322 pages, 1,237 chunks, and
1,237 valid 1,024-D vectors. Django is configured through the local `.env` for
the native database.

The new native snapshot
`20260814T223701+0530_native-windows-v1` is 10,763,722 bytes with SHA-256
`f3407b82d6922c00b899186e5bcfb8469d46f0716fb4e93c364912e381bc6c86`.
Its isolated restore passed all relational and vector checks, was removed, and
left production unchanged.

External acquisition is now genuinely local-first: Internet bytes are stored
under `Government_Documents/Incoming`, verification rereads only that local
file, and accepted exact bytes are promoted to `External_Official`. V2.2 ran
the same frozen 30 targets, found 12 candidates, validated nine local PDFs,
produced four exact matches, preserved two ambiguous groups, and archived one
new National Green Tribunal Act. The one-row V2.2 manifest uses only the
permanent archive. No ingestion occurred; searchable corpus, KG, vectors, and
retrieval semantics remain unchanged.

## KG Brain V2 (2026-08-15)

The frozen V2.2 NGT Act was ingested from `External_Official` as document 82:
23 digital pages, 30 chunks, and 30 1,024-D BGE-M3 embeddings. Archive SHA-256
remains `1530b1eb8370bee560559341cc53a93ae81e755bd3a04331a03ebc1e9208627f`;
replay returned `SKIPPED_UNCHANGED`.

The corpus is 72 documents, 1,345 pages, 1,267 chunks/embeddings. Two KG passes
yield 1,501 candidates and 91 relations (19 REFERENCES, 72 ISSUED_BY); the
second inserted zero rows. Of 66 strong signals, one explicit signal failed
parser validity, 62 are context-dependent, and three ambiguous. Strong edges
and HUMAN_CONFIRMED remain zero.

Topology is 19 document edges, 24 connected documents, 48 isolates, 57 weak
components, largest component 7, two bounded cycles, and maximum observed
simple directed depth 3. The 40-case Benchmark V2 has complete provenance and
all applicable metrics at 100%. All 29 dense ranks are unchanged. Evidence is
under `outputs/kg_brain_v2/`.
## KG-guided acquisition V1 (2026-08-15)

The frozen 50-target graph-completion queue is now executable through
`cli.kg_guided_acquisition_v1`. The campaign preserves rank and membership,
classifies parser/identity quality before network access, assigns bridge,
multi-cited, strong-relation, and standard tiers, and routes only to scoped
official adapters. The verified manifest is currently empty: strict local PDF
title extraction rejected a Water Act PDF for a target that omitted the
distinguishing `WATER` token, while the National Green Tribunal Act bytes were
already ingested. Maharashtra administrative targets remain primarily blocked
by the official GR CAPTCHA; the Divyang public index also presented a hostname
certificate failure during this run.

All downloaded PDF bytes are written first to
`D:/LLM_Project_data/Government_Documents/Incoming`, reopened locally for PDF,
SHA, identity, and duplicate checks, and only exact verified bytes may be
promoted to `Government_Documents/External_Official`. No acquired document was
ingested. Native PostgreSQL remains 72 documents, 1,345 pages, 1,267 chunks,
1,267 embeddings, 1,501 KG candidates, and 91 relations.

Routine task backups are disabled by user policy. This campaign created no
database backup and did not invoke Podman.
