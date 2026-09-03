# Dynamic Knowledge Graph Explorer V1

## Purpose and boundary

The explorer at `/knowledge-graph/` is an observation-only view of the current
native PostgreSQL knowledge graph. PostgreSQL remains the sole system of
record. The browser receives a fresh bounded JSON projection; it does not ship
or maintain an authoritative static graph, a Neo4j copy, or a vector-similarity
graph. This feature does not change retrieval behavior.

Only stored relations in `AUTO_VERIFIED` or genuine `HUMAN_CONFIRMED` state are
eligible. Candidate, unresolved, rejected, and unparseable rows are excluded.
All visualization repository transactions explicitly set
`transaction_read_only=on`, all graph API routes are GET-only, and there are no
create, edit, approve, or delete endpoints.

## Architecture

```text
native PostgreSQL 17 + pgvector
  -> GraphExplorerRepository (read-only SQL projection)
  -> GraphExplorerService (filters, topology, bounds, stable JSON)
  -> Django GET API under /knowledge-graph/api/
  -> server-rendered explorer + locally bundled Cytoscape.js
```

`DOCUMENT` and `DEPARTMENT` are the current node types. Directed edges support
`REFERENCES`, `ISSUED_BY`, `AMENDS`, `CORRECTS`, `SUPERSEDES`,
`PARTIALLY_SUPERSEDES`, `CANCELS`, and `EXTENDS`, including zero-count types so
the UI will expose their first future validated rows without an API change.
Graph relations are evidence-backed administrative facts, not vector
similarity.

## Running and access

With native PostgreSQL listening on `127.0.0.1:5434` and the normal project
environment loaded:

```powershell
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/knowledge-graph/`. The page offers explicit
**Refresh graph**, full-graph mode, focused 1/2/3-hop neighborhoods, incoming,
outgoing, or bidirectional traversal, deterministic node search, component
focus, and JSON export. Optional polling is off by default and can be set to
30 seconds, 60 seconds, or 5 minutes. Refresh keeps the current filters where
practical and shows the generation timestamp and fingerprint.

## Filters and future growth

Filters cover relation type, document kind, department, year range, validation
state, source origin, connected/isolated state, relationship presence, and
component. Full graph responses default to 500 nodes and 2,000 edges, enforce
hard caps of 5,000 and 10,000, and return explicit `truncated` and
`truncation_reason` fields. Focused neighborhoods provide a practical path as
the corpus grows beyond the current scale.

## Provenance inspection

Selecting a document shows identity, kind, date, department, source origin,
degree, incoming/outgoing relations, neighboring documents, stored path, SHA,
and available external-source provenance. Selecting an edge loads the complete
stored evidence chain separately from the compact graph payload:

```text
kg_relations
  -> kg_relation_evidence
  -> kg_reference_candidates
  -> source document + page + raw evidence
```

All evidence rows are returned for a validated `REFERENCES` edge. `ISSUED_BY`
uses the validated relation and stored document/department metadata.

## Offline frontend dependency

Cytoscape.js 3.34.2 is bundled in `static/vendor/cytoscape/` under the MIT
license. The template requires no remote JavaScript, CSS, fonts, graph API, or
CDN, so the explorer works with only the local application, static assets, and
PostgreSQL.

## API summary

- `GET /knowledge-graph/api/summary/`
- `GET /knowledge-graph/api/graph/`
- `GET /knowledge-graph/api/neighborhood/<document_id>/`
- `GET /knowledge-graph/api/search/`
- `GET /knowledge-graph/api/nodes/<node_id>/`
- `GET /knowledge-graph/api/edges/<relation_id>/`
- `GET /knowledge-graph/api/components/`

Use `download=1` on the graph endpoint for a logical filtered JSON export. It
is not a database backup. Evidence and integrity artifacts are under
`outputs/kg_visualization_v1/`; samples are examples only and never replace the
live API.
