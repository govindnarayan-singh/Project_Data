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

## KG Brain V3 (2026-08-24)

The frozen 50-target queue was revalidated and the 18 safe network-eligible
targets were routed through the official registry. India Code yielded two local
Incoming PDFs: the National Green Tribunal Act was already ingested and the
Water Act was an exact-identity mismatch. The verified manifest therefore has
zero rows (`b0cc6cfb5646db29db92a9f4d65ade9ee847d78dd1bf2b8b904c942257175308`),
so ingestion was safely skipped.

Two full KG reruns were idempotent. Live state remains 1,501 candidates and 91
relations (19 REFERENCES, 72 ISSUED_BY, zero strong edges). The lone explicit
supersession signal is not a parser defect: it identifies the prior decision by
date only, with no unique number or issuer. Topology remains 24 connected / 48
isolated documents, 57 components, largest component 7, and 2 cycles. Benchmark
V3 has 85 evidence-derived cases and all measured metrics pass. The next
bottleneck is official Maharashtra source coverage, not graph correctness.

## KG Brain V4 (2026-08-24)

V4 replayed the exact 50-row V3 priority artifact at SHA-256
`1bff4028b37fbb8117d0c75033f9b36aa29fe303235d06409f6fe49241a363fa`.
The source audit now separates kind support, issuer support, jurisdiction/year
support, measured network reachability, executable search, target queryability,
and confirmed source-family coverage. The old 47/50 claim was kind support
only; 18 identities remain quality-ready and 10 have at least one fully
queryable official route.

The generic reference parser now extracts explicit Marathi/English issuers and
persists corrected issuer fields on replay. Eight frozen targets gained issuer
evidence. A reusable Maharashtra Law and Judiciary Act List adapter was added
with official S3WaaS PDF allowlisting and state-jurisdiction gating. The live
campaign found one Finance candidate, staged and reopened it from Incoming,
and rejected it as an identity mismatch. The V4 manifest is therefore empty
(`b0cc6cfb5646db29db92a9f4d65ade9ee847d78dd1bf2b8b904c942257175308`);
no document, page, chunk, embedding, or archive file was added.

Two KG reruns were idempotent and preserved 1,501 candidates, 91 relations,
24 connected / 48 isolated documents, 57 components, largest component 7,
and closure 12/639. Benchmark V4 contains 85 evidence-derived cases with all
applicable metrics passing. Native PostgreSQL 17.11/pgvector 0.8.6 remained the
only runtime; Podman commands and database backups were both zero. Evidence is
under `outputs/kg_brain_v4/`.

## KG Brain V5 (2026-08-24)

V5 replayed the same frozen 50-target V3 queue and introduced deterministic
bounded-context parser version 2. The parser may use only the same sentence,
an adjacent sentence, an attached list item, or a structurally attached
heading. It does not use an LLM, embeddings, whole-page inference, or the
citing department as authoritative issuer evidence.

Quality-ready targets increased from 18 to 38. All 12 insufficient identities
and all eight damaged/unparseable identities were deterministically repaired;
five genuine version/identity ambiguities remain. Twelve targets have explicit
issuer evidence (eight in-reference and four bounded-context), while 38
citing-department values remain non-routing hints. The 120-row evidence-derived
parser regression passes all six metrics at or above 98.10% and is explicitly
not independent human ground truth.

The official registry now has 12 reusable families, adding Social Justice and
Special Assistance and Minority Development public document indexes. Eighteen
targets have at least one queryable route (21 target/source pairs), up from six
V4 identities/routes. The controlled live batch still produced only the same
Finance mismatch: it was reopened from Incoming, rejected, and never promoted.
The V5 manifest is empty, so ingestion was skipped.

Two native-PostgreSQL KG runs were idempotent. Corpus counts remain 72 / 1,345 /
1,267 / 1,267 and graph counts remain 1,501 candidates, 19 REFERENCES, 72
ISSUED_BY, and zero strong edges. Parser normalization reduces unique unresolved
targets from 627 to 622 without inventing a document closure. Topology remains
24 connected, 48 isolated, 57 components, and largest component 7. The 85-case
KG benchmark, archive integrity, database invariants, and retrieval fingerprint
all pass. No Podman command or database backup was used.

## KG Brain V6 (2026-08-25)

V6 froze the 38 V5 READY rows into 31 unique corrected identities and 30
network targets after exact local alias closure. Eight source families were
needed. The 12-source official registry was upgraded with explicit historical
coverage contracts, reusable Maharashtra S3WaaS document-index discovery, and
the migrated India Code DSpace 9.1 discovery/item/ORIGINAL-bundle API.

Six exact official Acts passed the local-first boundary and form the V6
manifest: three Maharashtra Acts and three central Acts. Every download was
written to `Government_Documents/Incoming` before PDF parsing, bounded local
OCR where needed, SHA/identity/duplicate verification, and byte-identical
promotion to `External_Official`. One officially indexed 2016 URL was rejected
because its local PDF was a different ordinance. The manifest contains no
ambiguous, mismatched, Incoming, remote, non-authoritative, or pre-ingested row.

Controlled ingestion added 6 documents, 74 pages, and 118 BGE-M3 chunks/vectors,
giving 78 / 1,419 / 1,385 / 1,385. KG run 1 added 175 candidates, 9 REFERENCES,
6 ISSUED_BY, and 16 evidence links; run 2 added zero rows. Final graph state is
1,676 candidates, 28 REFERENCES, 78 ISSUED_BY, 29 connected / 49 isolated
documents, 58 components, and largest component 10. Native PostgreSQL
17.11/pgvector 0.8.6 remained the only database runtime. Podman commands and
new backups were both zero. Evidence is under `outputs/kg_brain_v6/`.
'
## KG Brain V7 frozen-anchor closure (2026-08-31)

V7 freezes the pre-ingestion 78 searchable documents as the anchor corpus.
The immutable first-hop denominator is 1,103 eligible parser-V2 mentions and
709 exact canonical target identities. Six support documents were ingested
only because they close frozen anchor targets: five verified official Acts
from `External_Official` and one exact local original. The final searchable
corpus is 84 documents, 1,575 pages, and 1,647 BGE-M3 chunks/vectors at 1,024
dimensions.

Anchor target closure moved from 18/709 to 27/709 and anchor mention closure
from 86/1,103 to 171/1,103 under the documented exact-alias and local-SHA
rules. Operationally reachable closure is 27/258; high-value closure is
13/79. The global denominator expands normally to 1,947 mentions and 1,344
targets, but those new support-document references do not change the frozen
anchor denominator. They are reported separately as 841 mentions and 635
unique second-hop targets; acquisition of all second-hop targets was zero.

The KG now contains 2,588 candidates, 44 REFERENCES, and 84 ISSUED_BY edges.
Run two inserted zero candidates, relations, and evidence links. The 144-case
source-evidence KG benchmark and frozen 29-query dense benchmark pass; dense
metrics are exactly unchanged. The readiness gate recommends one more bounded
anchor-closure cycle because 22 high-value operationally reachable targets
remain unresolved. Native PostgreSQL was the only runtime; no Podman command
or database backup was used. Evidence is under `outputs/kg_brain_v7/`.

## V8 closure and Graph Retrieval V1 (2026-08-31)

V8 supersedes the older V7 next-step recommendation. It closed 3/22 bounded
high-value primary targets, moved high-value operational closure from 13/35 to
16/35, and assigned explicit exhausted, ambiguous, historical-gap, or mismatch
reasons to the remaining 19. No material untried official route remains, so the
authoritative recommendation is `MOVE_TO_GRAPH_AWARE_RETRIEVAL`; the old
approximately-80% threshold is not the final bounded policy.

The fixed retrieval experiment corpus is 87 documents, 1,588 pages, and 1,666
chunks/vectors. The KG has 2,631 candidates, 50 validated `REFERENCES`, 87
`ISSUED_BY`, and no strong document edge. Before/after document, retrieval-corpus,
and relation fingerprints are identical.

The unchanged frozen 29-query dense benchmark passes with document Recall@1
82.76%, Recall@3/5/10 96.55%, MRR 0.8966; page Recall@1 68.97%, Recall@3 93.10%,
Recall@5/10 96.55%, MRR 0.8190. The 40-row graph-specific source-evidence
benchmark found measurable one-hop recovery, but best-variant precision was
9/63 (14.29%). Production remains dense and the next recommendation is to
harden graph-aware retrieval before RAG. No acquisition, corpus/KG mutation,
Podman command, or backup occurred.

## Dynamic Knowledge Graph Explorer V1 (2026-09-02)

The `/knowledge-graph/` route is a live, read-only explorer backed by native
PostgreSQL. Its GET-only API exposes current summary counts, a bounded full
graph, validated 1/2/3-hop neighborhoods, deterministic search, document and
department details, component topology, and complete edge evidence. Manual
refresh is mandatory and optional polling defaults off. Cytoscape.js 3.34.2 is
bundled locally under MIT; no runtime CDN or remote graph service is required.

The verified baseline remains 87 documents, 1,588 pages, 1,666 chunks/vectors,
2,631 candidates, 50 `REFERENCES`, and 87 `ISSUED_BY`. Live document topology
is 38 connected / 49 isolated, 58 components, and largest component 15. The
stopped Recent-GR holdout package remains stopped: six partial output files
were preserved, with no partial code, manifest, selection, ingestion, or
database mutation. Evidence is under `outputs/kg_visualization_v1/`.

## Dynamic Knowledge Graph Explorer V2.1 recovery (2026-09-03)

The explorer is now a production-oriented graph-first workspace. It supports
collapsible application navigation, filters, summary, analytics, and inspector;
focus and true fullscreen modes; built-in Auto/Force/Hierarchy layouts;
zoom-aware labels; neighborhood emphasis; human-first inspection; documents-only
and administrative scopes; all/connected/isolated quick views; explicit
component selection; PNG and JSON export; and distinct empty, unavailable,
removed-selection, and truncation states. Cytoscape remains local and edge labels
remain off by default for the full graph.

The V1 node-count ambiguity is closed: 98 visible administrative nodes means 87
documents plus 11 active departments. The twelfth department registry row,
`www.indiacode.nic.in`, has no validated edge and is not materialized. The live
database remains 87 documents, 1,588 pages, 1,666 chunks/vectors, 2,631 KG
candidates, and 137 relations (50 `REFERENCES`, 87 `ISSUED_BY`). Document
topology remains 38 connected / 49 isolated, 58 components, largest 15.

V2 initially regressed in real browsers because unchanged static URLs allowed
cached V1 JavaScript and CSS to run against V2 HTML. The stale V1 script first
failed on the removed `[data-neighborhood]` control and then reproduced the
reported null `textContent` write to the removed `[data-generated-at]` widget;
the stale V1 CSS supplied the radial dot grid. V2.1 uses deterministic
`?v=2.1.0` explorer assets, restores the Neighborhood control, validates the
critical DOM contract, and isolates optional widgets from core graph loading.

Real Playwright/Chromium validation now loads 87 document nodes / 50 edges by
default and 98 administrative nodes / 137 edges, with zero `pageerror` or
`console.error` events. It physically exercises every panel and mode toggle,
1/2/3-hop directions, component/search/filter paths, node/edge provenance,
manual/auto refresh, exports, focus, fullscreen, Fit, Re-layout, and all five
loading/error states. Screenshots pass at 1366×768, 1440×900, and 1920×1080
without horizontal overflow; computed canvas, pseudo-element, and parent
background images are `none`.

All database, corpus, vector, candidate, relation, retrieval, ingestion, Podman,
and backup mutation counts are zero. The stopped Recent-GR holdout package was
not resumed. V2.1 evidence is under `outputs/kg_visualization_v21/`.

## Knowledge Graph Explorer V2.2 surgical UI patch (2026-09-03)

The application sidebar toggle now lives beside the Government Administrative
AI Copilot brand in the shared shell. It works on Knowledge Graph, Documents,
Unresolved, Corpus Health, Evaluation, and other shell-backed pages, preserves
state across navigation, and collapses to a usable 76-pixel icon rail. The
redundant graph-page toggle was removed; all V2.1 graph controls and behavior
remain otherwise unchanged.

Document nodes now derive a display-only role from trusted validated
`REFERENCES`: 49 isolated, 9 referenced-only, 11 citing-only, and 18 both.
The 11 active departments remain distinct diamond nodes. Candidate/unresolved
targets remain relation/evidence records and are not graph nodes. Shape plus
color communicates role, while tooltip, inspector, and legend explain the
same live contract. Playwright/Chromium cross-page and semantic-node acceptance
passes with zero page or console errors. The corpus/KG fingerprints and all
87 / 1,588 / 1,666 / 1,666 production counts remain unchanged. Evidence is
under `outputs/kg_visualization_v22/`; the Recent-GR benchmark remains stopped.

## Graph Retrieval V2 bounded hardening (2026-09-20)

The independent V2 service and 28-development/12-locked experiment are complete.
V1 reproduced 9/9 recoveries and 9/63 labeled addition precision. V2 development
retained 6/6 recoveries while reducing additions 46 to 14; its frozen one-shot
locked run recovered 2/3 and improved precision 3/17 to 2/5. Eleven unmatched
locked additions were removed, but one useful target was suppressed by the
document-ID-ordered one-addition cap. All accepted evidence has full relation
and separate content-page/chunk provenance. Locked P95 was 370.9 ms.

Recommendation: `ONE_MORE_RETRIEVAL_HARDENING_PHASE`, specifically a measured
relation-context relevance gate with frozen multi-label evidence judgments.
No post-test retuning, RAG, LLM, UI, acquisition or Recent-GR work occurred.
Dense remains production default; all 29 dense ranks/scores and the 87 / 1,588 /
1,666 / 1,666 corpus and 2,631-candidate / 137-relation KG fingerprints are
unchanged. Native PostgreSQL only; backups and Podman commands zero. Details:
`docs/GRAPH_RETRIEVAL_V2.md`, `outputs/graph_retrieval_v2/final_report.md`.

## Developer runtime hardening V1 (2026-09-21)

Local `python manage.py runserver` now ensures the existing native PostgreSQL
17 cluster before starting Django. `python -m cli.native_postgres ensure` uses
the same idempotent helper; status stays read-only and start aliases ensure.
Identity checks pin PostgreSQL 17 / 127.0.0.1:5434 / grdb / data17, with a
bounded startup deadline and cross-process lock. `AUTO_START_POSTGRES=false`
opts out; unset defaults off in CI or non-debug environments. Other Django
commands and production WSGI/ASGI entrypoints do not autostart the database.

The project-write root remains gov_ai_platform inside the parent Git repository.
AGENTS.md now explicitly permits normal existing-cluster runtime writes to
PostgreSQL/data17 and PostgreSQL/logs and defines scoped, non-repetitive task
validation. `cli.dev_preflight` provides the cheap reusable workspace/runtime
check. No installation, service registration, migration, backup, corpus or KG
mutation was performed. Graph Retrieval V3 remains stopped and was not resumed.
See `docs/DEVELOPER_RUNTIME.md` and `outputs/runtime_hardening_v1/final_report.md`.
