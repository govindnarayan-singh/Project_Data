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

The web process only approves and enqueues. The worker is a separate process;
it supports a bounded one-shot mode, a production-style polling loop, and exact
single-job troubleshooting:

```powershell
# Terminal 1 — web
python manage.py runserver

# Terminal 2 — worker
python manage.py process_document_ingestion_jobs --loop

# One controlled job
python manage.py process_document_ingestion_jobs --job-id <UUID>
```

`--poll-seconds` is bounded to 0.25–60 seconds and `--max-jobs` can bound a
loop. `--once` processes at most one eligible job. The worker is never started
inside a request handler or Django autoreloader. Production likewise runs one
web process class plus one or more separate worker processes.

PostgreSQL `transaction.atomic()` plus `select_for_update(skip_locked=True)`
prevents double claims. PENDING and transient RETRYABLE jobs are eligible while
the attempt count remains below the configured maximum. Attempts increment only
when claimed. A worker heartbeat records process start, latest heartbeat, and
current job while renewing the owned lease. Expired RUNNING jobs become
RETRYABLE and can be reclaimed, or FAILED after
DOCUMENT_INGESTION_MAX_ATTEMPTS. Scoped IAS users may also POST a
CSRF/version-checked bounded retry. Security/format, byte-integrity, rejection,
validation, and exhausted failures are permanent and never automatically
retried.

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
- DOCUMENT_INGESTION_STARTED
- DOCUMENT_INGESTION_RETRY_REQUESTED
- DOCUMENT_INGESTION_RETRIED (historical compatibility)
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

The Stage 3D integration PDF was confined to an isolated test database. The one
existing IAS-approved live submission was then processed through the same
durable worker command. Production is now 88 documents, 1,594 pages, 1,671
chunks/embeddings, 2,645 KG candidates, 50 REFERENCES, and 88 ISSUED_BY. The
new document contributes six OCR pages, five BGE-M3 vectors, fourteen
candidates, and one deterministic ISSUED_BY edge. Historical dense and Graph
V1/V2/V3 benchmark artifacts remain unchanged; their 87-document results are a
versioned historical baseline.

## Stage 3D durable worker closure

The worker independently rechecks the final `DOCUMENT_CORPUS_ADMISSION`
decision, subject/SHA/organization identity, the latest validation attempt, and
the exact quarantine SHA before archive promotion. Exact bytes are copied via a
temporary file, re-hashed, fsynced, and atomically renamed under
`Approved_Submissions`; IAS attestation is not relabelled as independent
official-source verification. The existing BatchIngestor performs pypdf/
pypdfium2/Tesseract `mar+eng` extraction, current chunking, BAAI/bge-m3
1,024-dimensional embedding, and atomic PostgreSQL persistence. Targeted KG
processing parses only the admitted document and identity-matching unresolved
evidence. Corpus admission never creates HUMAN_CONFIRMED or vector-derived
relations.

User-facing status is Waiting, Processing, Ingested, Retryable, or Failed.
Officers see plain-language progress and cannot retry; in-scope IAS users can
see bounded job/worker diagnostics and request an eligible retry. Job success
and submission INGESTED are committed only after stored page/chunk/vector
integrity and targeted KG completion.

## Stage 3B.1 PDF validation and provenance hardening

The validator is feature-aware and versioned as
`stage3b1-pdf-classifier-v2`. A PDF annotation is no longer itself a blocker.
Ordinary passive annotations and internal navigation are `SAFE_PASSIVE`;
AcroForm `/Sig` fields and signature widgets are `SAFE_SIGNATURE`; and
user-triggered HTTP(S) `/URI` actions are `SAFE_EXTERNAL_LINK`. External URLs
are never fetched during validation, and only bounded normalized domains are
stored. The final decision is deterministic: only controlled `BLOCK` findings
stop progression, while `WARNING` and `INFO` findings remain visible evidence.

JavaScript name trees and `/JavaScript` actions, `/Launch`, `/SubmitForm`,
`/ImportData`, unsupported `/Rendition`, embedded-file name trees,
`/FileAttachment`, embedded `/EF` streams, malformed structures, and unknown
executable action dictionaries fail closed under explicit reason codes. Action
chains, form trees, and feature counts have depth/count/time bounds. Size,
page, encryption, MIME, signature-byte, path, quarantine, and malware-scanner
controls remain unchanged. Bounded structural facts are retained on a later
failure: a safely counted page total is shown, while an unperformed count is
stored as null and displayed as “Not checked.” Preview states use explanatory
language rather than exposing `NOT_EXTRACTED` as the primary label.

Signature presence is not signature validity. The validator records bounded
field name, claimed signer name, PDF signing timestamp, filter, and subfilter,
but never stores or reports raw PKCS#7 bytes. This deployment has no supported
PDF-signature verifier, so signed files are labeled
`SIGNATURE_PRESENT_UNVERIFIED`. The UI must not say “verified” unless a future
policy-controlled cryptographic verifier successfully validates the signature
and trust chain.

Uploader role is derived from the authenticated access grant and stored as an
immutable snapshot. The shared provenance dropdown is removed. Officers may
provide a claimed source URL, claimed document identifier, and bounded source
context but cannot claim IAS verification. Scoped IAS users receive an explicit
source-attestation checkbox. That produces `IAS_ATTESTED_OFFICIAL_COPY`, which
is displayed separately from `AUTHORITATIVE_SOURCE_VERIFIED`; it is not an
independent source verification. Uploader identity, source claim, source
verification, digital-signature evidence, and corpus admission remain five
separate facts.

Every initial check and revalidation appends a protected
`DocumentValidationAttempt` containing validator version, timestamp, result,
bounded findings, page count, signature status, and external-link domains.
Existing rows were backfilled as `stage3-legacy-pdf-validator-v1`; legacy
failed zero-page snapshots became “Not checked” without changing their failed
decision. Revalidation is POST/CSRF/version protected and available only to the
authorized uploader or in-scope IAS user. It SHA-256 checks and reads the same
quarantined bytes, records `DOCUMENT_REVALIDATION_REQUESTED` and a new pass or
failure event, preserves earlier attempts/audit events, and can move a passing
file only to pending IAS approval. It never performs corpus ingestion.

Officer-facing cards lead with what happened, why it matters, and what to do.
IAS technical disclosure retains machine reason code, validator version, and
bounded structural evidence. Findings are grouped into blockers, review,
warnings, and informational evidence. Audit records retain exact machine codes
and hash-chain fields while audit presentation adds a human-readable reason.
