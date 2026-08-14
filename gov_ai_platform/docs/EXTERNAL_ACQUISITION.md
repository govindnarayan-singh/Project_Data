# External Administrative Document Acquisition V1.1

## Public-query boundary

External discovery sends only identity already printed in public government
documents: exact administrative number, optional date/issuer, or canonical
Act/Rule title and year. Queries are deterministic, at most three per target,
at most 240 characters, and are rejected if they contain a filesystem path,
database URL, hash, internal ID, configuration name, source text, or evaluation
data. No LLM, vector similarity, or full source-page text is used.

## Lifecycle and authority

`INGESTED`, `LOCAL_CATALOGUED`, `EXTERNAL_DISCOVERED`,
`EXTERNAL_VERIFIED`, and `NOT_FOUND` are separate states. An official result
is not a search document. Each target finishes as exactly one of
`AUTO_VERIFIED_READY`, `AMBIGUOUS`, `NOT_FOUND_AUTHORITATIVE`,
`BLOCKED_BY_SOURCE`, `BLOCKED_BY_ENVIRONMENT`, `DOWNLOAD_FAILED`,
`IDENTITY_MISMATCH`, or `UNPARSEABLE_REFERENCE`.

The registry records source ID/name, authority class, jurisdiction, supported
kinds and strategies, base host/domains, discovery/metadata/download
mechanisms, schemes, rate policy, and enablement. Enabled sources are limited
to `OFFICIAL_PRIMARY`, `OFFICIAL_SECONDARY`, or
`TRUSTED_GOVERNMENT_INDEX`; non-authoritative sources cannot be admitted.

Current sources:

- `gr.maharashtra.gov.in`: OFFICIAL_PRIMARY. The search CAPTCHA is not bypassed,
  so controlled automated targets finish `BLOCKED_BY_SOURCE`.
- `indiacode.nic.in`: OFFICIAL_PRIMARY. V1.1 supports exact legal-title search,
  official landing-page resolution, and official `/bitstream/` PDFs.
- `egazzete.mahaonline.gov.in`: OFFICIAL_PRIMARY. Its stateful form is not
  accessed through undocumented endpoints.

## Adapter and security architecture

Each adapter implements `supports`, `build_queries`, `search`,
`fetch_candidate_metadata`, `download`, and `verify_authority`. The KG resolver
contains no site-specific scraping.

The network client enforces source-specific domain and HTTPS allowlists,
rejects credentials, localhost, literal or DNS-resolved private/loopback/link-
local/reserved addresses, validates every redirect, limits redirects, response
bytes, timeout, retries, exponential backoff, and request rate, and maintains
separate atomic body/metadata caches. HTML named `.pdf` is rejected by
Content-Type and `%PDF`; accepted files must also parse with at least one page.
Server filenames are ignored.

Downloads enter configured `EXTERNAL_INCOMING_ROOT` with deterministic safe names.
SHA-256 and PDF page count are recorded. Files are immutable and deduplicated;
the manifest rejects any SHA already ingested or present in the 2,715-file
local catalogue.

## Identity and AUTO_VERIFIED

Administrative documents require exact normalized numbers with deterministic
date/issuer corroboration where available. Acts and Rules require canonical
title, kind, and year. Official landing metadata must match, and the downloaded
PDF probe must contain the canonical title tokens and year. Subject similarity
and BGE-M3 are forbidden identity evidence.

`AUTO_VERIFIED` requires an allowed official source, MATCH_EXACT/MATCH_STRONG,
a valid locally parsed PDF, safe Incoming path, complete landing/document/query
provenance, and no local/ingested SHA collision. Multiple distinct official
files for one identity are `VERSION_VARIANT` and remain ambiguous. No automatic
path may create `HUMAN_CONFIRMED`.

## Controlled V1.1 result (2026-08-14)

The same frozen 20 targets were attempted: 13 Maharashtra GR/order/circular/
letter/corrigendum targets and seven Acts. Public network access was available.
The 13 administrative targets were `BLOCKED_BY_SOURCE` because CAPTCHA access
was not bypassed. India Code produced 21 official candidate rows across all
seven Act targets.

Five Acts passed every gate and are in
`data/manifests/external_verified_targets_v1.csv`: Maharashtra Zilla Parishads
and Panchayat Samitis Act 1961, Environment (Protection) Act 1986, Air
(Prevention and Control of Pollution) Act 1981, Maharashtra Urban Areas
Protection and Preservation of Trees Act 1975, and Maharashtra Slum Areas Act
1971. The Rights of Persons with Disabilities Act remained ambiguous because
India Code exposed distinct original and amended-version PDFs. The generic
`AMENDMENT_ACT_2021` target mismatched multiple unrelated Acts.

Manifest SHA-256 is
`03fbb7f0dd07b28b714cbb7ba938b0544797f74fa3f69f46d99ddeab9fa0424a`.
Replay inserted zero catalogue rows and reproduced the same manifest SHA.

This phase does not ingest the manifest. Documents/pages/chunks/embeddings
remain 66/1,044/765/765. Known Availability Closure changes from 7/252 to
12/252; all ingestion- and graph-based closure metrics remain unchanged.
Evidence is under `outputs/external_acquisition_v1_1/`.

## Next ingestion gate

The next separately authorized step is the five-row AUTO_VERIFIED manifest:
production batch ingestion, BGE-M3 embeddings, deterministic KG rerun, closure
measurement, integrity checks, and frozen retrieval regression. Until that
gate, the external catalogue and Incoming remain non-search storage.
## Production ingestion closure (2026-08-14)

The frozen five-row manifest (`03fbb7f0dd07b28b714cbb7ba938b0544797f74fa3f69f46d99ddeab9fa0424a`) was ingested without another discovery run. All five rows are `ACT`, `OFFICIAL_PRIMARY`, and `MATCH_EXACT`. Two accepted V1.1 rows omitted by the earlier catalogue upsert were reconciled from the already-frozen `verified_targets.csv`; their earlier failed/rejected rows were retained and no network source was contacted.

The acquisition files remain immutable in the approved quarantine root. Each new `EXTERNAL_OFFICIAL` document links to one acquisition candidate and has complete lifecycle history through INGESTED. Replay is idempotent: all five return `SKIPPED_UNCHANGED`, with no new pages, chunks, embeddings, or documents. Evidence is in `outputs/external_ingestion_v1/`.

The next 30-target priority file is planning only. The 13 earlier `BLOCKED_BY_SOURCE` targets all remain `CAPTCHA_REQUIRED` for the Maharashtra GR filtered search; controls were not bypassed and no alternate existing official adapter covers those administrative kinds.

## Persistent archive policy (V2)

`EXTERNAL_INCOMING_ROOT` is staging only. A file cannot become a future
ingestion source until authority and identity gates pass and the exact bytes are
promoted to `EXTERNAL_DOCUMENT_ROOT`. The archive uses root identity
`EXTERNAL_OFFICIAL`, controlled kind directories, safe identity-derived
SHA-suffixed filenames, and relative PostgreSQL paths. Remote filenames remain
provenance only.

Promotion verifies Incoming SHA, copies without re-saving, verifies SHA,
size, byte equality, and PDF parsing at the destination, and never overwrites
different bytes. Same-SHA destinations are reused. `ARCHIVE_FAILED` is excluded
from manifests. Document provenance retains both Incoming evidence and the
permanent archive link.

The archive is authoritative project data, not cache. A portable backup must
include `D:/LLM_Project_data/GRs`, `EXTERNAL_DOCUMENT_ROOT`, a PostgreSQL
backup, and the application repository/configuration.

## Controlled V2 result (2026-08-14)

The 30-target run produced 17 official candidates and six valid quarantine
downloads. Two Rights Act groups remain `AMBIGUOUS_VERSION`; six Act fragments
mismatch and six have no authoritative result. Sixteen administrative targets
remain `BLOCKED_BY_SOURCE` because CAPTCHA was not bypassed. No new target
passed final archival gates, so `external_verified_targets_v2.csv` is an empty,
valid manifest. Replay used cache, inserted zero catalogue rows, and reproduced
its SHA.

## V2.1 hardening and replay

V2.1 preserves the exact 30-target artifact. Legal discovery requires a
structured Act/Rule title plus year and rejects footnote/prose fragments before
network access. A full canonical title ending with the structured target
title/year may match deterministically; year changes never match. Principal
Acts, Amendment Acts, consolidated texts, version variants, and ambiguous
versions remain distinct.

`divyang_sahayak` is an `OFFICIAL_SECONDARY` adapter for the public Maharashtra
Divyang Sahayak government-resolution index. It is reusable only for disability
administration, requires an exact listed date, then verifies the identifier in
the PDF probe. It does not call the CAPTCHA-protected GR form. The public index
and matching documents were independently observed, but the project runtime
could not reach that host during replay, so it resolved zero targets. No CAPTCHA
or stateful control was bypassed.

The controlled official-response replay produced nine candidates across five
targets. Three title aliases resolve to already-ingested archived India Code
Acts using existing provenance and archive SHA; they are not new downloads or
manifest rows. Six Rights Act PDFs across two target groups remain
`AMBIGUOUS_VERSION`. The National Green Tribunal candidate could not complete
metadata/download access. Eight malformed Act targets are unparseable. The
V2.1 manifest is empty, replay inserts zero catalogue rows, and no ingestion,
embedding, or graph change occurs.

## V2.2 local-first discovery (2026-08-14)

The current acquisition boundary is:

```text
Internet → Government_Documents/Incoming → local verification
  → Government_Documents/External_Official → later authorized ingestion
```

Incoming bytes are stored atomically before PDF parsing or identity work. The
stored file is reread and hash-checked, and all document/identity validation is
performed from that local copy. Promotion copies the exact bytes to a safe,
immutable, kind-scoped archive path and verifies byte equality. Incoming never
appears in an ingestion manifest.

V2.2 replayed the unchanged 30-row target file at SHA-256
`8d14c5a80c6cc618cf73f64493f80fac6457ee3bec27aad222b58e7a9d2725a8`.
Five issuer-scoped official Maharashtra department adapters were added for
Finance, Parliamentary Affairs, Environment, Marathi Language, and IT; the
existing Divyang and India Code adapters remain strict. The run found 12
official candidates across eight targets, locally validated nine PDFs, kept two
version groups ambiguous, and rejected two mismatches. Four candidates matched
exactly; three were already-ingested aliases and one new National Green
Tribunal Act passed every archive gate.

`data/manifests/external_verified_targets_v2_2.csv` therefore contains exactly
one permanent `EXTERNAL_OFFICIAL` document. Replay inserted zero catalogue
rows, archive integrity passed for six files, and no ingestion, page creation,
chunking, embeddings, or graph mutation occurred.
