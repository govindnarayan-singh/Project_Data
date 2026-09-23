# Stage 1 — Identity and access foundation

## Decision and scope

Stage 1 keeps Django's built-in `auth.User`. The project had no
`AUTH_USER_MODEL`, auth tables, sessions, login, or application profile before
this stage. Replacing the user model after the corpus/KG application already
existed would add migration risk without improving the required boundary, so
roles and organizational authority are additive `identity_access` records.

The migration creates only Django auth/session metadata and these application
tables: organization units, access grants, IAS authority scopes, access
requests, request transitions, one-time activation tokens, and bounded
rate-limit buckets. Corpus, page, chunk, embedding, candidate, relation,
retrieval, and visualization schemas are untouched.

## Roles and organizational scope

- `IAS_OFFICER` is an explicitly bootstrapped privileged grant. An IAS user
  has one or more separately versioned authority scopes. Being IAS is not by
  itself permission to decide requests outside those scopes.
- `OFFICER` is created only by a scoped IAS approval. A public form contains no
  role field and rejects privilege-shaped parameters.
- Grants have `ACTIVE`, `SUSPENDED`, `REVOKED`, and `EXPIRED` states, start and
  optional expiry timestamps, an organization, and a version. Authorization
  reads the current database state on every protected request.
- Multiple units and multiple IAS authorities are supported. Scope expiry and
  a future delegation flag are modeled without implementing delegation UI.

All existing search, document, PDF, graph, graph API, unresolved, corpus, and
evaluation routes are protected by deny-by-default middleware. Anonymous users
are sent to login (APIs return 401); authenticated users without a current
active grant are denied (APIs return 403). IAS queue/actions add object-level
scope checks. Reusable helpers live in `apps.identity_access.permissions`;
future-stage permission hooks return false until their stages define policy.

## Access request state machine

The deterministic request path is:

`REQUESTED -> PENDING_VALIDATION -> PENDING_IAS_APPROVAL -> APPROVED | REJECTED | EXPIRED | CANCELLED`

The request and the access grant are separate records. Submission creates no
user and no grant. Server-side validation produces one of
`ALLOW_TO_QUEUE`, `REJECT_INVALID`, `REJECT_DUPLICATE`, `RATE_LIMITED`,
`COOLDOWN`, or `MANUAL_REVIEW_REQUIRED`. A configured email-domain mismatch is
visible as a manual-review risk flag; it never auto-approves or silently hides
the request.

Every request transition records old/new state, actor, time, reason, and
version. IAS decisions require POST and CSRF, lock the request transactionally,
verify pending state, compare the submitted version, check current scope, and
reject self-decision. There is no bulk approval. Approval creates or reactivates
only an `OFFICER` grant and produces a one-time activation handoff.

### PostgreSQL approval/rejection locking

The concurrency object is the `AccessRequest` base row. Approval, rejection,
and expiry enter `transaction.atomic()` and use one shared helper with
`select_for_update(of=("self",))`. The lock query does not join nullable
`requester_user`; separation-of-duties uses the already-loaded
`requester_user_id`, and organization scope is checked by ID. This avoids
PostgreSQL's invalid `FOR UPDATE` over the nullable side of an outer join while
preserving the row lock, version check, state check, and IAS scope check.

`requester_user` means an existing authenticated identity requesting access on
its own behalf. Anonymous pre-activation officer requests leave it null.
Already-authorized users are blocked from the public request form, and an
authenticated unprivileged user's submitted email must match the signed-in
identity. Genuine self-approval and self-rejection remain prohibited.

## Queue and abuse resistance

Request fields are bounded by model/form limits. Email and official identity
are normalized server-side. Alias stripping occurs only for domains explicitly
listed in `ACCESS_ALIAS_NORMALIZATION_DOMAINS`; no provider or government domain
is hardcoded. Conditional database uniqueness prevents concurrent pending
requests for the same normalized email or identity. Rejected identities have a
configurable cooldown, pending requests expire, and the IAS queue uses indexed
filters plus 25-row server-side pages.

Throttles are layered by source, normalized email, and normalized identity.
Login throttles use both source and account/source keys with short windows and
temporary cooldowns; errors do not reveal whether an account exists. Bucket
keys are HMAC digests, expired buckets are deleted opportunistically, and no
Redis or new service is required.

`REMOTE_ADDR` is authoritative by default. `X-Forwarded-For` is considered
only when `REMOTE_ADDR` belongs to an explicit CIDR in
`ACCESS_TRUSTED_PROXY_NETWORKS`; the chain is walked from the trusted edge and
malformed headers are ignored. Do not configure that setting until the actual
reverse-proxy topology is known.

## Activation, passwords, sessions, and revocation

Passwords use Django's configured hashers and validators. Neither approval nor
bootstrap accepts or generates a default password. Activation secrets are
random, single-purpose, expiring, stored only as keyed SHA-256 digests, and
invalidated after use. The plaintext link is displayed once to the trusted IAS
operator for secure handoff; it is not stored in request history or logs.

The development email backend remains Django's console backend, but Stage 1
does not claim or simulate delivery. Production must configure an approved
SMTP/identity/SSO handoff before email delivery is used.

Django rotates the session key at login and flushes it at logout. Session
cookies are HttpOnly and SameSite=Lax; CSRF protection is on; session lifetime
defaults to eight hours and browser-close expiry. `Secure` cookies and HTTPS
redirects are environment settings so local HTTP remains usable. Production
must set a strong `DJANGO_SECRET_KEY`, explicit hosts, TLS termination,
`DJANGO_SECURE_COOKIES=true`, and `DJANGO_SECURE_SSL_REDIRECT=true`.
`DJANGO_DEBUG=true` is for local development only; production must set
`DJANGO_DEBUG=false`. Logs and task reports must not copy database credentials,
session or CSRF secrets, activation secrets, the Django secret key, or full
environment dumps.

Suspension, revocation, grant expiry, scope expiry, and inactive users are read
from PostgreSQL for each protected request. Existing sessions therefore lose
protected access on their next request without relying on a long-lived
authorization cache.

## IAS bootstrap

There is no public IAS registration route. After migration, an operator creates
the first IAS identity explicitly:

```powershell
python -B manage.py bootstrap_ias `
  --username <operator-chosen-username> `
  --email <official-email> `
  --organization-code <stable-slug> `
  --organization-name "<official unit name>" `
  --base-url http://127.0.0.1:8000
```

The command refuses duplicate username/email identities and conflicting unit
codes, creates a non-staff/non-superuser account with an unusable password,
creates the scoped IAS grant, and prints one activation URL. Copy that URL only
through an authorized secure channel. The command has no web route and no
default credentials. Stage 1 automated tests use an isolated in-memory test
database; no fake IAS or officer was added to live `grdb`.

## Validation and next stage

Focused validation uses:

```powershell
$env:DJANGO_TEST_SQLITE='true'
python -B manage.py test tests.test_identity_access --noinput
python -B manage.py test tests.test_web tests.test_portal_ui tests.test_graph_explorer --noinput
```

The live deployment uses `DATABASE_URL` at native PostgreSQL
`127.0.0.1:5434/grdb`. `DJANGO_TEST_SQLITE` is test-runner isolation only and
must not be set in the server environment.

Stage 2 is deliberately not implemented. It will generalize the minimum
access-request transition history into governance-wide immutable audit events
and approval controls.
