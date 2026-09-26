# Stage 3 — Secure Document Intake and IAS Corpus Approval

Status: **complete** (2026-09-24)

## Boundary and trust model

Stage 3 implements secure PDF intake only. Upload is not trust, and IAS corpus
admission is not independent authoritative-source verification. Four controls
remain explicit and separately visible:

1. file security and malware-scan status;
2. document identity and duplicate status;
3. source/provenance classification;
4. IAS admission approval followed by durable ingestion.

Stage 4 unresolved-reference UI, Stage 5 query-time assistance, V3 human review,
RAG, Neo4j, a second vector database, and acquisition changes are out of scope.

## Authorization and visibility

An active OFFICER grant permits submission only for that grant's organization.
An active IAS_OFFICER grant plus live IASAuthorityScope permits
submission/review only inside that authority scope. Anonymous, ungranted,
pending, suspended, revoked, expired, or cross-scope access fails closed.

Before admission, only the uploader and an IAS user authorized for the
submission organization may view status or fetch validated bytes. Quarantine is
never a static/media URL. Preview/download endpoints resolve the generated
storage key server-side and return application/pdf, nosniff, private no-store
caching, sandbox CSP, and controlled content disposition. PDFs that failed
structural validation are not served.

## Storage and original bytes

The exact upload is streamed once, bounded by DOCUMENT_MAX_UPLOAD_BYTES, and
hashed immediately with SHA-256. User filenames are reduced to metadata-only
basenames. Storage always uses:

Government_Documents/Incoming/Officer_Uploads/<submission UUID>/original.pdf

Accepted bytes are never rewritten. Bounded preview text and detected metadata
are derivative fields. Rejection and validation failure retain quarantine
evidence; the worker never deletes it.

Approved uploads are hash-verified, copied to a temporary destination,
re-hashed, fsynced, and atomically renamed under:

Government_Documents/Approved_Submissions/<submission UUID>/original.pdf

The archive key is recorded before heavy ingestion. Officer/IAS approval does
not move a file into External_Official. That directory remains reserved for
bytes independently verified through the established official-source process.

## PDF security

Only application/pdf with a .pdf extension and leading %PDF- signature is
accepted. pypdf must parse the document. Empty, zero-page, encrypted,
malformed, oversized, over-page-limit, embedded-file, JavaScript, document
action, page action, and annotation-action PDFs are rejected. Direct preview
extraction is limited by configurable page, character, and elapsed-time
bounds; upload-time OCR is prohibited.

Windows Defender was detected locally and is supported by the scanner adapter
without installation. The default AUTO configuration selects its CLI with
remediation disabled. Deployments may explicitly configure Windows Defender or
ClamAV. If no scanner is configured, status is exactly
MALWARE_SCAN_NOT_CONFIGURED—never CLEAN. Structural PDF validation still runs.

Relevant settings:

- DOCUMENT_MAX_UPLOAD_BYTES (default 25 MiB)
- DOCUMENT_MAX_PAGES (default 500)
- DOCUMENT_PREVIEW_PAGES (default 3)
- DOCUMENT_PREVIEW_CHARACTERS (default 6000)
- DOCUMENT_VALIDATION_TIMEOUT_SECONDS (default 30)
- DOCUMENT_MALWARE_SCANNER (AUTO, NONE, WINDOWS_DEFENDER, CLAMAV)
- DOCUMENT_MALWARE_SCANNER_EXECUTABLE
- DOCUMENT_QUARANTINE_ROOT
- APPROVED_SUBMISSION_ROOT

## Submission state and duplicate behavior

Implemented submission states are:

- VALIDATION_FAILED
- PENDING_IAS_APPROVAL
- REJECTED
- QUEUED_FOR_INGESTION
- INGESTING
- INGESTED
- INGESTION_FAILED

Security and validation are separate controlled fields. Provenance is one of
AUTHORITATIVE_SOURCE_VERIFIED, IAS_VERIFIED_COPY,
OFFICER_PROVIDED_UNVERIFIED, or UNKNOWN; direct officer submissions are forced
to the unverified officer classification. The UI never permits a user to
self-designate an upload as independently authoritative.

Duplicate outcomes are NEW, EXACT_PENDING_DUPLICATE,
EXACT_CORPUS_DUPLICATE, and POSSIBLE_IDENTITY_DUPLICATE. Exact pending or corpus
bytes are displayed and stopped before approval. A partial unique constraint
prevents concurrent active submissions for one SHA-256. Possible identity
matches remain visible evidence for IAS review and are not silently discarded.

The optional origin hook is controlled to DIRECT_UPLOAD, UNRESOLVED_REFERENCE,
or QUERY_MISSING_REFERENCE; only direct upload is wired in Stage 3.

## Governance

DOCUMENT_CORPUS_ADMISSION is registered through the Stage 2 fail-closed policy
registry. Officer uploads always require an authorized IAS decision and
officers can never self-approve. IAS uploads also stop at pending approval and
require a separate deliberate action.

DOCUMENT_IAS_SELF_CONFIRM_ALLOWED=true supports the current single-IAS local
environment, but is weaker than two-person maker-checker. Setting it to false
requires another scoped IAS user. Both modes are tested.

Decision requests carry case and subject versions. One database transaction:

- locks and revalidates case, subject, role, scope, state, digest, and version;
- inserts the append-only GovernanceApprovalDecision;
- appends governance and document audit events;
- transitions rejection with no job, or admission to queued;
- creates exactly one DocumentIngestionJob.

Any audit or job insert failure rolls back the decision and submission state.

## Durable ingestion

python manage.py process_document_ingestion_jobs --once claims at most one
pending job. --job <UUID> permits a controlled target. There is deliberately no
implicit loop and no runserver/autoreload coupling.

PostgreSQL select_for_update(skip_locked=True) prevents double claims. A
bounded lease detects worker crashes. Expired jobs become RETRYABLE, or FAILED
after DOCUMENT_INGESTION_MAX_ATTEMPTS. Only scoped IAS may POST a
version-checked retry. Security/format, hash, rejection, and exhausted failures
are not automatically retried.

Immediately before processing, the worker recomputes the quarantine SHA-256.
It then reuses BatchIngestor, which retains current pypdf/pypdfium2/Tesseract
(mar+eng, 300 DPI), metadata, TokenChunker, BAAI/bge-m3 (1,024 dimensions), and
atomic PostgresStorage behavior. APPROVED_SUBMISSION is an additive document
origin resolved only under the approved-submission root. Same-byte and
same-path ingestion remains idempotent.

## Targeted knowledge graph processing

After document persistence, KnowledgeGraphPipeline.run_targeted() parses only
the new document's pages. The existing deterministic resolver uses the full
document identity index, then reprocesses only unresolved candidates whose
normalized identifier can match the new document. Persistence touches those
candidates and missing ISSUED_BY/validated REFERENCES evidence only; it does
not run a full candidate rebuild or promote strong relations. No vector
similarity creates a legal or administrative edge.

The job records new candidates, new-document resolutions, matching existing
candidate resolutions, still-unresolved count, and inserted evidence/edge
counts.

## Audit and failures

The Stage 2 ledger records:

- DOCUMENT_SUBMITTED
- DOCUMENT_VALIDATION_FAILED
- DOCUMENT_READY_FOR_APPROVAL
- DOCUMENT_APPROVED
- DOCUMENT_REJECTED
- DOCUMENT_INGESTION_QUEUED
- DOCUMENT_INGESTION_RETRIED
- DOCUMENT_INGESTION_SUCCEEDED
- DOCUMENT_INGESTION_FAILED

One server-generated correlation UUID connects submission, approval case,
decision, job, and audit events. Metadata uses the Stage 2 allowlist and never
contains PDF text, original bytes, credentials, stack traces, or filesystem
paths. Ordinary users receive bounded safe failure codes/reasons; internal
details remain restricted operational data.

## Shared UI hardening

Stage 3A.1 makes the shared shell a two-row viewport grid: a 64-pixel top
header spans the application, while the sidebar, main workspace, and optional
context panel occupy independently constrained regions below it. The body no
longer owns page scrolling. Navigation and main content each have an explicit
`min-height: 0` scroll boundary, so a short viewport cannot push Governance,
Audit Log, or Approval Cases outside the dark navigation surface.

The sidebar now contains only brand, collapse control, section labels, and
authorized navigation. Username, human-readable role, and the CSRF-protected
POST sign-out action live in an accessible top-header account menu. The menu
closes on outside click or Escape. At 900 pixels and below, navigation becomes
an off-canvas drawer with a dismiss overlay and Escape handling; compact header
identity preserves account access. Desktop collapse remains a true 76-pixel
icon rail, with labels removed from layout, accessible names/tooltips retained,
and the released width immediately returned to the main workspace.

The shell uses shared width/height tokens, a restrained scrollbar, visible
active and focus states, and reduced-motion support. The context panel remains
independent and Knowledge Graph retains its intentional zero-width context
column. Stage 3 adds Submit document/My submissions links without page-specific
shell duplication.

Approval Cases now has an intentional empty state and human document
presentation. Audit list rows show usernames/System, display labels, friendly
resource names, compact identifiers, and a horizontally safe responsive table;
the detail page retains exact machine values and full UUIDs.

No in-app browser surface was connected during final validation. HTTP/template
tests cover the DOM contract. Manual visual checklist: expanded/collapsed shell
at 1366×768 and narrow width; account/footer containment; Audit Log filters and
rows; Approval Cases empty and document states; Submit document; submission
status; IAS approval evidence/actions.

## Validation and production invariant

SQLite unit/security/authorization/governance/audit/UI tests, real PostgreSQL
claim/recovery tests, and an isolated actual PDF → extraction → chunking →
mocked 1,024-dimensional model boundary → pgvector persistence → targeted KG
test pass. The isolated pipeline rerun is SKIPPED_UNCHANGED.

No test PDF entered the live corpus. Stage 3 production tables start empty.
Production remains 87 documents, 1,588 pages, 1,666 chunks/embeddings, 2,631 KG
candidates, 50 REFERENCES, and 87 ISSUED_BY.
