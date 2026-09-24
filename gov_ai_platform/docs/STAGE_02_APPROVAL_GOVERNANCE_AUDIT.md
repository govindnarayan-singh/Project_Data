# Stage 2 — Approval Governance and Immutable Audit

Status: **DONE** (2026-09-23)

Stage 2 adds a reusable approval foundation and a cross-system governance audit
ledger. It does not replace domain state. `AccessRequest`, `AccessGrant`, and
`IASAuthorityScope` remain the Stage 1 access-control source of truth, and
`AccessRequestTransition` remains the domain-specific transition history.

## Approval governance

`GovernanceApprovalCase` records the workflow type, organization, submitter,
subject identity/version/digest, case state/version, optional expiry, and
timestamps. The implemented states are `PENDING`, `APPROVED`, `REJECTED`, and
`CANCELLED`. `GovernanceApprovalDecision` is a separate append-only record with
the actor and organization snapshots, decision, bounded reason, and the case and
subject versions observed by the reviewer. A database uniqueness constraint
allows only one final decision per case.

The explicit in-process policy registry maps a controlled workflow type to an
`ApprovalPolicy`. A policy supplies the reviewer role, self-approval rule,
organization-scope validator, allowed decisions, and a server-side subject
snapshot reader. Unknown workflow types fail closed. No document-intake or V3
policy is registered in Stage 2; tests use a temporary isolated policy only.

Case decisions lock the case row, load actor authority and current subject state
server-side, enforce maker-checker separation and organization scope, compare
expected case and subject versions/digests, and write the decision, domain case
transition, and audit event in one transaction. A stale case or subject is
rejected. Future HTTP decision endpoints must remain authenticated, POST-only,
CSRF protected, and service-backed; Stage 2 deliberately provides no generic
decision endpoint or future document/review UI.

## Audit event and chain

`GovernanceAuditEvent` stores a UUID, monotonic sequence, schema version,
timestamp, controlled category/action/outcome, scalar actor/role/organization
snapshots, resource identity/version, a server-generated correlation UUID,
bounded reason/state fields, sanitized metadata, origin/provenance fields, and
the previous and current hashes. The controlled categories are
`IDENTITY_ACCESS`, `APPROVAL`, `AUTHORIZATION`, `SECURITY`, and `SYSTEM`.

`record_governance_event()` is the only supported application insertion path.
It locks the singleton `GovernanceAuditChain` row, allocates the next sequence,
serializes the security-relevant fields as sorted compact UTF-8 JSON with UTC
microsecond timestamps, computes SHA-256, inserts the event, and advances the
chain head in one transaction. The head lock serializes the intentionally low
governance-event volume and prevents divergent concurrent branches.

Application model/queryset guards reject event and decision updates/deletes.
PostgreSQL triggers independently reject `UPDATE` and `DELETE` on both tables;
normal inserts, migrations, test-database lifecycle, and backup/restore remain
available. `python manage.py verify_audit_log` explicitly checks sequence
continuity, previous hashes, recomputed hashes, and the stored chain head.
`python manage.py audit_health` reports count, last sequence/time, and cheap head
consistency without rescanning the full ledger.

The ledger is application-append-only, PostgreSQL-trigger protected, and
tamper-evident. It is not mathematically immutable against an unrestricted
PostgreSQL superuser who can disable triggers and rewrite both events and the
head. External WORM storage or signed anchoring would be a separate future
control. A keyed HMAC was not added because this deployment does not yet have
independent audit-key management; reusing or hardcoding an application secret
would overstate protection.

## Correlation, privacy, and bounded growth

`GovernanceCorrelationIdMiddleware` creates a random UUID for every request and
returns it as `X-Request-ID`; client-supplied request IDs are ignored. Governed
views pass that server value into domain services. Management commands create
their own operation UUID.

Audit metadata uses an explicit key allowlist, at most 16 keys, 256 characters
per string, 20 primitive list items, no nested structures, and at most 4,096
serialized bytes. Human reasons are plain text, limited to 1,000 characters,
secret-shaped assignments are redacted, and templates use Django escaping.
Passwords, activation values, sessions, CSRF values, authorization headers,
database credentials, environment dumps, and request bodies are not recorded.

The ledger is not an HTTP access log. Page views, searches, authentication
failures, anonymous denials, and rate-limit bucket activity do not create audit
rows. Valid queued access requests are governance-significant and audited; the
existing rate limit prevents one row per repeated source request. Bounded
service denials currently create no event, so failed attempts cannot be used to
grow the immutable table. Operational security logs remain separate.

Retention defaults to indefinite. Stage 2 has no destructive cleanup path.
Administrative export was omitted: the IAS pages are paginated and there is no
unbounded dump or CSV-formula-injection surface. A future export must be
scope-filtered, date/row bounded or streamed, formula-safe, and itself audited.

## Stage 1 integration

The following successful Stage 1 actions now append audit events in the same
database transaction as their domain mutation: access request queued, approval,
rejection, grant creation/reactivation, suspension, revocation, activation
completion, and IAS bootstrap. Required audit failure raises a controlled domain
error and rolls back the high-impact action. Existing row locks, optimistic
versions, self-decision prevention, organization scope, CSRF, POST-only decision
views, and activation security remain intact.

`python manage.py import_stage1_audit_history --dry-run` assesses existing
`AccessRequestTransition` provenance. `--apply` imports only those transitions,
uses deterministic unique source identifiers, marks origin as
`HISTORICAL_STAGE1_IMPORT`, preserves the original time separately, and records
the actual import time. Replay is idempotent. Older grant/bootstrap/activation
history lacks equivalent provenance and is not fabricated. The first audit-row
creation time is a permanent cutover: later live-era transitions are excluded
from every historical replay, preventing duplicate relabeling.

## IAS interface and authorization

The Governance navigation is visible only to IAS users and links to a paginated
50-row audit list and approval-case list. Audit filters cover date range,
category, action, actor ID, resource type, and outcome. Every list/detail query
intersects the server-derived Stage 1 organization scope; browser organization
parameters cannot widen it, and out-of-scope detail IDs return 404. Officers
have no Stage 2 audit interface and receive 403 from governance routes.

## Operations and evidence

Apply schema with `python manage.py migrate governance`. Verify the full ledger
with `python manage.py verify_audit_log`; use `python manage.py audit_health` for
the lightweight summary. Stage evidence is in
`outputs/stage_02_governance_audit/`.

The migration is additive. Corpus/vector/KG/retrieval schemas, all retrieval
implementations and artifacts, the Knowledge Graph Explorer, and the paused V3
review dataset were not changed. Stage 3 is the next authorized stage.
