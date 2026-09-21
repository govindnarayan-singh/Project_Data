# Graph Retrieval V2: bounded precision hardening

Status: **CONDITIONAL PASS — ONE_MORE_RETRIEVAL_HARDENING_PHASE**.
Production remains `RETRIEVAL_MODE=dense`. V2 is experimental and is not
authorized as an LLM/RAG evidence source. No RAG or LLM was implemented.

## Frozen corpus and boundaries

Native Windows PostgreSQL 17.11 / pgvector 0.8.6 at `127.0.0.1:5434/grdb`
contains 87 documents, 1,588 pages, 1,666 BGE-M3 1,024-dimensional vectors,
2,631 KG candidates, 50 REFERENCES and 87 ISSUED_BY relations. The six strong
relation types remain zero. The stopped existing native instance was started;
no restore, migration, backup, Podman, acquisition, ingestion, corpus embedding,
KG mutation or Explorer change occurred. Recent-GR remains stopped.

Corpus SHA: `ebd956d83e7070a181ce33b295874e1739e877693fed31449035b6ee44f1f7a7`.
KG SHA: `09fbd3129ee0aacc41d21243c6cf01aa6255529da5530698831e93fcfe3c3dd4`.
Full stored-row fingerprints additionally cover documents, pages, chunks,
candidates, relations and evidence. Protected-file hashes include V1 sources
and artifacts, dense retrieval, graph sources, UI and Recent-GR partial files.

## Why V1 was insufficient

The actual rerun reproduced 9/9 recoveries but only 9/63 single-label matched
additions (14.29%). Relationship existence is not query relevance. V1 sources,
CLIs, original benchmarks and outputs were not changed or regenerated.

## Service contract

`knowledge_engine.retrieval.graph_aware_v2.GraphAwareRetrievalV2` accepts a query,
unchanged dense Top-K (3/5/10), `graph_mode=auto` or an explicit diagnostic mode,
a request cap that may only reduce the policy cap, and a diagnostics flag.
It returns untouched dense results, an intent decision and effective graph
decision, selected seeds, supplemental graph documents, dense corroborations,
optional rejected-candidate diagnostics, provenance and component timings.

`PostgresGraphV2Repository` reuses V1's parameterized one-hop REFERENCES query
and existing document-restricted cosine chunk lookup. Every connection is
explicitly read-only and the endpoint must be the native project database.
V2 validates trust and provenance again at the service boundary. Candidates,
unresolved edges, ISSUED_BY, stronger relations, self-links, incomplete evidence
and edges not adjacent to selected seeds never enter the evidence set.

There is no score fusion, reranking, dense-result displacement, new graph
schema, vector-created edge, second graph database, or web integration.

## Intent and direction

`graph_v2_intent.py` defines a small deterministic, versioned rule catalogue.
Precedence is explicit opt-out, unsupported legal-effect question, explicit
broad reference lineage, then incoming/outgoing reference intent. Conflicting
or absent relational cues return NO_GRAPH. Bidirectional is reserved for
explicit reference/administrative history, not the default.

Examples include outgoing “which referenced document” / “under which Act” and
incoming “which documents cite this Act” / “follow-up documents”. Amendment,
supersession and legal-validity requests return NO_GRAPH because REFERENCES
does not support those claims. Even diagnostic/ablation flags cannot bypass
the legal-effect or explicit-opt-out guard. Temporal words select a reference
direction but do not prove that a cited document is earlier/later in date.

Mixed English/Marathi benchmark questions use their English relational cue.
No unvalidated Marathi direction catalogue was invented: ambiguous Marathi-only
requests conservatively keep dense evidence. Full Marathi intent coverage is
not claimed. Rule IDs, cues, negatives, precedence and examples are recorded in
`query_intent_rules.json`.

## Selected policy and the unresolved relevance defect

The frozen policy selects the first unique dense document. Top-3/5 did not
recover any additional development target and increased unmatched additions.
Seed cosine floors 0.4/0.5/0.6 did not improve discrimination; margin gates
0.02/0.05 lost useful recoveries. No numerical seed-confidence gate was kept.

Every new graph candidate receives a best-chunk lookup restricted to its own
document, reusing the query embedding. A nonempty matching chunk, positive
page range, chunk ID and finite content similarity are mandatory. Relation
evidence page/text stays separate from content page/text/chunk ID.

Development compared no score gate, absolute floors 0.35/0.45/0.55, seed-relative
floors and dense-tail requirements. Absolute floors recovered 6/4/1 of the six
eligible cases; the alternatives also lost recovery before separating the
unmatched candidates reliably. Therefore `candidate_strategy=none` means
**no calibrated semantic score threshold**, NOT “skip the content lookup”.
Content presence is not a semantic relevance certificate. Numeric-threshold
sensitivity is not applicable to the selected threshold-free policy; all
coarse trade-offs are retained.

The smallest development-recovery-preserving cap was one new document per
seed and one total. Candidate ordering remains deterministic seed rank,
relation type, document ID, relation ID, direction; it is not a relevance
ranking. Dense duplicates become `ALREADY_IN_DENSE_GRAPH_CORROBORATED`, retain
their dense rank and edge provenance, and do not count as additions. Additional
neighbors and cap rejections remain visible in diagnostics.

## Split, freeze and one-shot discipline

The original 40-row source-evidence benchmark was preserved byte-for-byte.
Before tuning, identical queries were grouped (37 groups) and assigned by a
deterministic SHA-ordered subset algorithm approximating 70/30 strata totals.
The split is 28 development / 12 locked rows, with 6 / 3 recovery cases,
13 / 5 negative-addition cases and 17:11 / 8:4 outgoing:incoming labels.

Split SHA: `48a5e491e0583d50b94192b590c4dd239c483cf6de3dd1ebcd1dd8b5d2431207`.
Policy SHA: `5d136808d0afcf82a7126a9e99616ee4f9e6c5bd3070918d0d1b12be6f255d97`.

V1 was measured before tuning, including a locked baseline. Only development
outcomes were inspected during tuning. Rules, policy, evaluator/service source
hashes, ordering, caps, development evidence and readiness criteria were frozen
before the one locked V2 run. An exclusive test-start ledger prevents replay;
development commands reject any frozen policy. No post-test retuning occurred.

These are `AUTO_VERIFIED_SOURCE_EVIDENCE` engineering labels, **not independent
human ground truth**. Each question has only one useful-document label even
when multiple citing documents may genuinely answer it. “Unmatched” is not
proof of irrelevance. All 40 questions are relational: empirical NO_GRAPH and
bidirectional-intent accuracy cannot be estimated. Unit tests exercise those
contracts but are not accuracy estimates. Corpus/target identities overlap
across the split; identical query strings do not. No real-world, legal or
production-accuracy claim is warranted from this small existing benchmark.

## Results and readiness

| Population | V1 recovery | V2 recovery | V1 labeled precision | V2 labeled precision | Additions/query V1 → V2 |
| --- | --- | --- | --- | --- | --- |
| Development (28) | 6/6 | 6/6 | 6/46, 13.04% | 6/14, 42.86% | 1.643 → 0.500 |
| Locked (12) | 3/3 | 2/3 | 3/17, 17.65% | 2/5, 40.00% | 1.417 → 0.417 |

Locked unmatched additions fell from 14 to 3. Direction accuracy was 100%,
wrong-direction paths zero, and accepted-document provenance complete. Locked
warm-model total latency was mean 252.1 / median 222.8 / P95 370.9 ms; model
initialization was separately logged (~10 seconds). Final policy measurement
used fresh embeddings and SQL, not cached query results. Development ablations
reused identical query embeddings/dense results and restored their original
measured costs; graph/restricted SQL still executed.

The fixed cap rejected the useful target document 34 for GRV1-039 after adding
document 29. The rejected target had content similarity 0.55345; it was rejected
by `TOTAL_CAP`, not by intent, missing seed, trust or absent graph coverage.
This is a held-out failure, not a justification to adjust the frozen cap.

The dense 29-query document/page metrics, ranks and Top-10 scores are unchanged.
The graph-RAG gate fails recovery protection and candidate-relevance safety.
Do not authorize graph-augmented RAG from a precision increase caused largely
by document-ID truncation. Dense-only RAG is technically possible but was not
implemented or newly authorized here.

Next bounded work should evaluate a binary query-specific evidence gate at the
validated relation page/context (not just the best whole-document chunk), with
frozen multi-label judgments for every returned candidate and a new split.
Measure useful recovery and noise reduction attributable to the gate itself,
not arbitrary ID ordering. Do not reuse this locked test for tuning.

## Developer workflow and evidence

`python -m cli.graph_retrieval_v2_eval` has `initialize`, `baseline`, `develop`,
`validate`, `freeze`, `test`, `finalize` and `report` stages. The first stages
have already run: **do not delete the freeze/test ledger to rerun them**.
Use a separately authorized versioned experiment for further development.
`finalize` performs only dense/integrity rechecks; `report` reads frozen results.
The frozen finalizer has a reporting-only date-serialization comparison defect:
loaded JSON dates are strings while freshly measured rows initially hold Python
date objects. The independent `python -m cli.graph_retrieval_v2_dense_audit`
compares the saved before/after artifacts in the same representation, proves all
29 Top-10 records equal except latency, and preserves the original false flag
in `dense_regression_pre_serialization_audit.json`. Run that audit after any
finalize recheck. Frozen service/evaluator sources and the locked run were not
altered to correct this reporting artifact.

All evidence is under `outputs/graph_retrieval_v2/`; the split is also stored at
`data/retrieval_eval_graph_v2_split.csv`. Outputs are locally retained and
excluded by the project's existing `outputs/*` gitignore policy. The main test
suite passed 409 tests plus 12 subtests; seven opt-in browser/network or legacy
mutation integration cases were skipped. Two new native PostgreSQL integration
tests are read-only; legacy insert/update/rollback tests were not activated.

```powershell
$env:RUN_GRAPH_V2_POSTGRES='1'
$env:PGOPTIONS='-c default_transaction_read_only=on'
python -m pytest -q tests/test_graph_retrieval_v2.py tests/test_graph_retrieval_v1.py
```
