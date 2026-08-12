# Knowledge Graph plan

## Status and evidence base

Knowledge Graph extraction is not implemented. The 2026-08-13 diagnostic made
no database changes and studied all 61 controlled documents and 1,022 stored
pages with deterministic rules. Full evidence is under
`outputs/knowledge_graph_diagnostic/`.

The diagnostic found 414 candidate mentions in 57 documents, 42 observed
pattern families, one exact in-corpus resolution, one ambiguous resolution,
301 NOT_IN_CORPUS candidates, and 111 unparseable candidates. A 50-candidate
Codex source-text review measured 96.00% detection precision, 93.33% type
accuracy, 63.33% document-number extraction accuracy, and 82.76% date accuracy.
Recall is not yet measured. Resolution precision was 100% for only one resolved
sample and is not statistically adequate for production claims.

The diagnostic decision is **CONDITIONAL PASS**: a small graph is viable, but
identifier parsing, metadata verification, weak-page review, and independent
human validation are prerequisites.

## Observed reference evidence

Observed reference document kinds are:

- Government Resolution
- Circular and Government Circular
- Notification
- Act
- Rule
- Government Letter and Department Letter
- Government Order
- Court Order
- Scheme or policy document
- Meeting Minutes
- Corrigendum
- Supplement

Observed structures include `संदर्भ`, `वाचा`, and English `Reference`
sections, numbered lists, inline mentions, tables/annexures, and mixed
Marathi-English references. Identifiers vary in Devanagari/ASCII digits,
spacing, slashes, hyphens, `प्र.क्र.` spelling/spacing, department abbreviations,
English `No.`/`G.R.`, and OCR punctuation.

Relation-language evidence exists for REFERENCES, AMENDS, SUPERSEDES, CANCELS,
CORRECTS, and EXTENDS. These are signals, not graph edges. Most require wider
context, target linking, and human legal/administrative review.

## V1 entity model

### Core entities

| Entity | Purpose | Canonical identity | Important attributes | Source of truth | Resolution |
|---|---|---|---|---|---|
| AdministrativeDocument | One logical entity for all observed document kinds | Ingested: PostgreSQL document ID + SHA-256; unresolved: candidate key | kind, raw/normalized number, raw/normalized date, title/subject, issuer, corpus status | Stored document metadata plus source evidence | Unique normalized identifier, corroborated by date/issuer; never subject similarity alone |
| Department | Issuing administrative department | Controlled canonical department key/name | canonical name, observed aliases | Verified document metadata/controlled vocabulary | Exact alias mapping with review for new aliases |

`GovernmentResolution`, `Circular`, `Notification`, `Act`, `Rule`, letter,
order, court order, scheme/policy document, minutes, corrigendum, and supplement
are V1 values of `AdministrativeDocument.kind`. This avoids a premature physical
table hierarchy while preserving observed distinctions.

External references remain first-class unresolved document candidates. Their
raw evidence and values must never be overwritten by normalized representations.

### Future entities

- AdministrativeSubject: subject text exists, but the corpus does not establish
  stable canonical subject identity.
- Scheme/Programme concept: distinct from a scheme document only after stable
  programme identity extraction exists.
- IssuingAuthority below department, legal provision/section, person/role, and
  geographic/applicability entities: potentially useful but not yet supported
  by measured deterministic extraction.

## V1 relation model

| Relation | Source → target | Required evidence | Automatic creation | Human confirmation |
|---|---|---|---|---|
| REFERENCES | AdministrativeDocument → AdministrativeDocument/unresolved candidate | Explicit mention with source page and evidence | Candidate: yes; AUTO_VERIFIED edge only for unique deterministic resolution | Required for ambiguous/weak evidence, optional audit for exact match |
| ISSUED_BY | AdministrativeDocument → Department | Verified issuing department metadata/evidence | Yes for verified stored metadata | Required for suspicious/missing issuer data |

Future relations:

| Relation | Evidence requirement | Automation policy |
|---|---|---|
| AMENDS | Explicit amendment language, resolved target, complete context | Candidate detection only; human confirmation mandatory |
| SUPERSEDES / PARTIALLY_SUPERSEDES | Explicit supersession scope and resolved target | Human confirmation mandatory |
| CANCELS | Explicit cancellation of the target document, not a generic cancelled item | Human confirmation mandatory |
| CORRECTS | Corrigendum/correction explicitly tied to target | Human confirmation mandatory |
| EXTENDS | Explicit duration extension tied to target decision/scheme | Human confirmation mandatory |
| APPLIES_TO | Stable target entity plus explicit applicability evidence | Defer until entity model exists |
| RELATED_TO | Explicit evidence-based semantics | Omit from V1; never derive from vector similarity |

## Candidate, provenance, and validation model

Future PostgreSQL storage should be candidate-first. Required fields include:

- source document ID and source page;
- nullable resolved target document ID;
- relation type and referenced document kind;
- raw evidence text;
- raw and normalized reference number;
- raw and normalized reference date;
- extracted issuer/department;
- extraction pattern and method;
- resolution status, method, evidence, and candidate count;
- confidence category;
- validation status, reviewer, review note, and review time;
- created/updated timestamps.

Use validation states `CANDIDATE`, `AUTO_VERIFIED`, `HUMAN_CONFIRMED`,
`REJECTED`, and `UNRESOLVED`. Strong legal/administrative relations never become
authoritative solely from a rule, model, or numeric confidence score.

## Normalization and resolution strategy

1. Preserve every raw value and evidence span.
2. Normalize Unicode, digits, whitespace, slash/hyphen punctuation, common
   Marathi number labels, and observed English labels into a separate matching
   key.
3. Parse observed numeric, Marathi-month, and English-month dates.
4. Resolve by exact normalized number.
5. Corroborate or narrow with date and issuer/department.
6. Return AMBIGUOUS for collisions or conflicts; do not guess.
7. Return NOT_IN_CORPUS for valid unmatched identifiers and retain them for
   acquisition planning.
8. Return UNPARSEABLE when no defensible identifier exists.

Subject or vector similarity cannot establish document identity.

## Unresolved-reference strategy

The diagnostic produced 254 unique normalized NOT_IN_CORPUS groups, but parser
review shows this is an upper bound containing malformed identifiers. Rank
candidates by number of distinct citing documents, total mentions, identifier
completeness, date/issuer corroboration, and page quality. Human-review the
normalized group and original evidence before locating or ingesting a PDF.

The first review queue should include references cited by multiple documents:
`अपंग2013/प्र.क्र.201/अ.क.2`, `अभियान2021/प्र.क्र.57/तां.क.1`,
`इडीडी2003/प्र.क्र.340/भाग3/सुधार2`, `विमंअ2008/प्र.क्र.59/पाच`, and the Rights
of Persons with Disabilities Act, 2016 variants.

## Existing `gr_relations`

Recommendation: **REPLACE_WITH_MIGRATION** after schema review.

The placeholder provides source/nullable target FKs, relation type, target GR
number, page/text evidence, and float confidence. Its primary key and unique
source/type/target/page constraint are useful primitives, but it lacks the
candidate lifecycle, raw/normalized values, reference date/type/issuer,
extraction and resolution provenance, validation workflow, reviewer metadata,
timestamps, and multiple-evidence semantics required by V1. It must remain
unchanged until a reviewed migration is approved.

PostgreSQL remains the system of record. A separate graph database is not
justified for V1.

## Vector retrieval and graph traversal

Dense retrieval identifies semantically relevant passages. The graph identifies
explicit, validated administrative connections. A future request flow is:

1. dense retrieval returns evidence chunks and source documents;
2. graph traversal expands from those documents through validated edges;
3. evidence and validation state accompany every expanded document;
4. ranking combines query relevance with explicit-edge evidence without
   converting semantic similarity into an edge.

## Future document-detail UI

Display verified GR/document metadata, outgoing references, incoming
references, unresolved candidates, and separately labelled potential semantic
relations. Each item exposes source page, short evidence, resolution method,
and validation status. Reviewers can confirm/reject candidates; ordinary users
must not see unreviewed semantic relations as authoritative.

## Implementation stages

1. Verify seven HIGH-impact metadata diagnostics and review/selectively repair
   the 14 IMPORTANT_BAD pages without bulk re-ingestion.
2. Improve deterministic identifier boundaries, self-reference rejection,
   mixed-reference splitting, ACT/RULE canonicalization, and English `Dt.` date
   parsing.
3. Create an independently human-labelled precision/recall and resolution set.
4. Review a candidate-first PostgreSQL migration and constraints/indexes.
5. Implement an idempotent preview extractor that writes no authoritative edges.
6. Add review workflow and persist candidates with full provenance.
7. Enable AUTO_VERIFIED REFERENCES and verified ISSUED_BY only after quality
   gates are met.
8. Introduce human-confirmed semantic relations individually based on measured
   signal quality.
9. Add graph-aware retrieval and the document-detail UI after edge validation.

