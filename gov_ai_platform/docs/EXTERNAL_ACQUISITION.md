# External Administrative Document Acquisition V1

## Lifecycle

`INGESTED`, `LOCAL_CATALOGUED`, `EXTERNAL_DISCOVERED`, `EXTERNAL_VERIFIED`, and
`NOT_FOUND` are different states. An official web hit is not a search document.
READY_FOR_INGESTION requires deterministic MATCH_EXACT/MATCH_STRONG identity,
official provenance, a valid immutable PDF/SHA-256, and AUTO_VERIFIED state.
This work package does not ingest external files.

## Official registry

- `gr.maharashtra.gov.in`: OFFICIAL_PRIMARY GR repository; filtered search has
  a case-sensitive CAPTCHA which automation does not bypass.
- `indiacode.nic.in`: OFFICIAL_PRIMARY India Code for Acts and Rules.
- `egazzete.mahaonline.gov.in`: OFFICIAL_PRIMARY Maharashtra e-Gazette; V1 uses
  no undocumented endpoint for its stateful form.

NON_AUTHORITATIVE mirrors and document-sharing sites are disabled. GR identity
uses exact normalized identifier/date/issuer evidence; Acts and Rules use
canonical title/year/kind. Vector and subject similarity are forbidden. Queries
are deterministic and bounded; no LLM is used.

## Security

Only allowlisted HTTPS domains are accepted. Credential URLs, cross-domain or
excessive redirects, oversized responses, unbounded retries, HTML masquerading
as PDF, unsafe filenames, and path traversal are rejected. PDFs require both
`application/pdf` and `%PDF`. Server filenames are ignored and verified files
are immutable under `data/acquisition/quarantine`.

## Current batch

Twenty complete high-priority identities were selected from 237 eligible
unresolved targets. The environment rejected live discovery because it would
transmit private-corpus-derived identifiers without separate approval. The
offline run persisted 20 `BLOCKED_BY_EXTERNAL_DATA_APPROVAL` rows, zero
downloads/verifications, and an empty
`data/manifests/external_verified_targets_v1.csv`. Replay inserted zero rows.
