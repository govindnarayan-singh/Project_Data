# Stage 4 - Unresolved Reference Workbench

Status: **Stage 4F conditional pass; owner audit-discontinuity acceptance pending** (2026-10-04)

## Scope and boundary

Stage 4 materializes unresolved knowledge-graph references into a durable,
organization-scoped operational queue. It does not change query-time retrieval,
run historical benchmarks, add vector-derived graph truth, or implement any
Stage 5 behavior.

The workbench is an operational layer. `kg_reference_candidates` remains the
authoritative source of reference identity, source document/page, raw mention,
resolver state, and evidence. `kg_relations` remains the authoritative graph.

## 4A - target registry

`UnresolvedReferenceWorkItem` groups candidate rows by a deterministic SHA-256
fingerprint of organization scope plus normalized reference identity, kind,
date, and department. `UnresolvedReferenceCandidateLink` retains the original
candidate IDs instead of copying or replacing candidate evidence.

Eligible source states are `NOT_IN_CORPUS`, `AMBIGUOUS`, `NEEDS_REVIEW`, and
`UNPARSEABLE`. Resolved, self-reference-rejected, and rejected-noise rows are
excluded. The work types are missing document, ambiguous match, incomplete
identity, parsing review, and document-exists-but-unresolved.

The recovery command is:

```text
python manage.py sync_unresolved_reference_workbench [--dry-run]
```

It is idempotent and does not write KG relations. At the final dry run it saw
2,003 eligible candidates, 79 candidates mapped to the sole configured
organization, 1,924 candidates without a safe Stage 1 organization mapping,
and 62 target groups. It proposed zero creates, updates, links, or reassignments.
The unmapped candidates are deliberately not exposed across organizational
boundaries.

## 4B - evidence, scope, and assignment

Active officers can see and claim items in their granted organization. Scoped
IAS users can see, claim, release, assign to an in-scope active officer, and
close invalid items. Every list, detail, and mutation rechecks scope server-side.
Claim, release, and assignment use PostgreSQL transactions, row locks, and an
expected version. Claims expire after the configured 30-minute interval, while
activity history is retained.

The detail view reads bounded authoritative evidence and provides source
document/page links. The list uses persistent rows, server-side filtering,
search, sorting, counts, and pagination; it never rebuilds the KG on page load.

## 4C - corpus and local matching

The sync uses deterministic normalized identity matching against the production
document table and `local_document_catalog`. It does not use embeddings or
title similarity to create an edge. The browser submits only a catalog record
ID. The server resolves its stored relative path and enforces the configured
document-root policy; arbitrary browser-supplied paths are not accepted.

The current scoped queue has two exact local-catalog matches, no corpus matches,
and 60 items with no local match. Catalog data is queried from PostgreSQL; page
requests do not scan the 2,715 PDFs.

## 4D - Stage 3 handoff

Both an uploaded PDF and a selected local-catalog record enter the existing
Stage 3 workflow through `submit_document`. The origin is
`UNRESOLVED_REFERENCE`, and `origin_reference` is the work-item UUID. The local
path is resolved server-side and exact bytes are copied through the existing
quarantine, SHA-256, PDF validation, provenance, IAS approval, and durable
worker flow. Active submissions are deduplicated per work item.

After a linked ingestion succeeds, the Stage 3 worker invokes a bounded callback
that refreshes only the linked work item and invokes targeted KG processing. An
ingested document is not, by itself, proof that a reference has resolved.

## 4E - automatic and human resolution

Automatic resolution is available only for an exact deterministic corpus
match and runs the existing targeted KG pipeline. It never runs a full KG
rebuild.

An officer may propose an existing corpus document, but this does not mutate the
graph. The proposal opens the Stage 2 `KG_REFERENCE_RESOLUTION` workflow. An
in-scope IAS maker-checker decision is required; IAS self-confirmation is denied
by default. Approval creates `REFERENCES` relations with
`HUMAN_CONFIRMED` validation and preserves the source candidate, page, raw
evidence, approver, governance case, and resolution method. Rejection preserves
the proposal and leaves the KG unchanged. Vector similarity is never sufficient
to create a relation.

## 4F - synchronization, audit, and UI

Significant transitions are audit actions: claim, release, assignment, document
submission, automatic resolution, human proposal, approval/rejection, and
invalid closure. Read-only page views do not create audit events. The original
unresolved-candidate URL safely redirects to the aggregated work item.

The responsive list and detail pages expose human-readable states, availability,
submission progress, resolution history, bounded evidence, and next actions.
An authenticated rollback-only production smoke rendered both pages with HTTP
200; no temporary identity, grant, scope, or session row remained afterward.

## Validation

- focused Stage 4 and portal suite: 25 passed;
- relevant Stage 2/3/4 regression suite: 73 passed and 15 subtests passed;
- PostgreSQL double-claim race: exactly one claimant succeeds;
- Django system check: clean;
- migration drift check: clean;
- Python compilation: clean;
- final sync dry run: zero changes;
- corpus/KG integrity: 89 documents, 1,598 pages, 1,676 chunks/vectors,
  2,654 candidates, 50 `REFERENCES`, and 89 `ISSUED_BY`;
- canonical document fingerprint:
  `4c6ea7cf686b006b79af037495945133e22fe4c22a89ce589640f07afafe86a3`;
- canonical graph fingerprint:
  `6b4734228dd675e7eea9472aed1db949c789bd20722028e2abdda454314edc99`.

The 89-document baseline was observed before Stage 4 implementation and is one
document newer than the 88-document estimate in the work package. Stage 4 did
not add a corpus document or relation.

## Production acceptance blocker

During Stage 4 development, a direct pytest invocation used `grdb` before the
repository had a safe pytest/Django database bootstrap. Django's test flush
removed pre-existing Django-managed rows. The custom corpus, candidate, graph,
catalog, page, chunk, and embedding tables were not removed.

The test harness now provisions and uses isolated `test_grdb`, and the captured
organization plus the 62/79 Stage 4 registry rows were rematerialized. However,
the original users, access grants, IAS scopes, Stage 2 audit history (previously
through sequence 27), approval cases, Stage 3 submission records, and ingestion
job records cannot be reconstructed faithfully from the available evidence.
They have not been fabricated. The live audit verifier therefore reports a
cryptographically valid empty chain, which is not historical-continuity PASS.

Stage 4 remains at 4F until a verified post-Stage-3 backup restores those
operational tables, or the owner explicitly authorizes identity re-bootstrap
and accepts a documented audit-history discontinuity. Stage 5 must not begin
before that decision and a repeated production acceptance check.

## Stage 5 boundary

No search/Copilot query result consumes Stage 4 state yet. Stage 5 may add
query-time provenance and missing-document assistance only after the 4F recovery
gate is closed.

## Stage 4F production acceptance (2026-10-04)

Production acceptance was rerun after the owner recreated one IAS identity and
one Officer identity. Both users are active, have active grants for organization
`d001`, and the IAS user has an active authority scope. Authenticated live HTTP
checks returned 200 for both roles on the workbench list and detail pages.
Search, filtering, four-page pagination, evidence, local availability, and
role-appropriate controls rendered correctly. Read-only requests added zero
audit events.

The corpus had legitimately advanced before this acceptance run to 91
documents, 1,607 pages, 1,687 chunks/vectors, 2,673 candidates, 50 REFERENCES,
and 91 ISSUED_BY. Seventeen newly scoped candidate groups were therefore
materialized through the normal idempotent sync, moving the workbench from
62/79 work items/links to 79/96. A subsequent dry run reported zero writes and
there are no duplicate target fingerprints.

The Officer claimed and released one safe open work item through normal
CSRF-protected HTTP actions. The item returned to OPEN and unassigned at version
3; audit sequences 49 and 50 record the claim and release. The current 50-event
audit epoch verifies successfully. Corpus/KG counts and fingerprints were
identical before and after acceptance.

Automated tests now have a fail-fast production database guard. Both pytest and
the Django test runner resolve to explicit `test_grdb`; an attempted effective
test name of `grdb` raises a fatal configuration error before database setup.
The focused acceptance suite passed 76 tests and 15 subtests on `test_grdb`, and
production identity/workbench counts were unchanged by the test run.

Historical audit events 1-27 were not restored. The current chain is a valid
new epoch beginning with the recreated IAS bootstrap, not continuous historical
evidence. Current status is therefore:

`AUDIT_CONTINUITY = DISCONTINUOUS_PENDING_OWNER_ACCEPTANCE`

Stage 4F is a conditional pass with one remaining blocker: explicit project
owner acceptance of the documented audit-history discontinuity. Stage 5 remains
unauthorized until that acceptance is recorded.
