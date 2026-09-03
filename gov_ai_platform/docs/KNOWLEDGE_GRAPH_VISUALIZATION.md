# Dynamic Knowledge Graph Explorer V2.2

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

Open `http://127.0.0.1:8000/knowledge-graph/`. V2 makes the graph canvas the
primary workspace. The application navigation toggle beside the Government
Administrative AI Copilot brand is owned by the shared application shell, works
on every shell-backed page, collapses to a 76-pixel icon rail, and preserves the
expanded/collapsed preference in browser storage. Filters, summary, insights,
and the selection inspector can collapse independently. Focus mode (`F`) removes
secondary UI; true browser fullscreen targets the complete graph workspace and
uses a CSS maximized fallback if the Fullscreen API is rejected. `Escape`
exits focus or fallback fullscreen. Optional polling remains off by default and
can be set to 30 seconds, 60 seconds, or 5 minutes.

The initial page does not require Apply or Refresh: it shows a loading state
with non-authoritative dash counts, fetches the live graph immediately, and
then publishes the returned counts. Loading, empty graph, filtered-to-zero,
API error, and client-render error are distinct states with appropriate reset
or retry actions.

The recommended initial presentation is **Documents** with **All** components:
all 87 documents remain accounted for while `REFERENCES` topology stays
readable. **Administrative** mode adds the 11 departments that participate in
validated `ISSUED_BY` edges. The registry contains 12 rows, but its inactive
`www.indiacode.nic.in` row is not a graph node. Therefore the complete
administrative graph is exactly 87 document nodes + 11 active department nodes
= 98 nodes, with 137 validated edges.

## Filters and future growth

Filters cover relation type, document kind, department, year range, validation
state, source origin, connected/isolated state, relationship presence, and
component. Full graph responses default to 500 nodes and 2,000 edges, enforce
hard caps of 5,000 and 10,000, and return explicit `truncated` and
`truncation_reason` fields. Focused neighborhoods provide a practical path as
the corpus grows beyond the current scale.

Component selection supports all, largest, or any selected component. Layout
choices use built-in Cytoscape algorithms only: Auto uses force-directed layout
for full graphs and concentric layout for neighborhoods, Force increases
separation, and Hierarchy uses directed breadth-first placement. Edge labels
are off in the full graph by default and on in focused neighborhoods. The mini
status bar always distinguishes total graph counts from currently displayed
counts. Search uses human display labels, centers the selected result, and opens
the inspector.

## Provenance inspection

Selecting a document shows its useful title, subject, kind, date, department,
source origin, page and relation counts first. Canonical identity, path, hash,
extraction details, and other technical provenance remain available in an
expandable section. Selecting an edge loads the complete
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

The V2.2 template references `explorer.css?v=2.2.0` and
`explorer.js?v=2.2.0`; the shared shell similarly versions its portal assets.
This deterministic release version prevents a browser
from pairing cached V1 explorer assets with the breaking V2/V2.1 DOM contract;
it is intentionally stable within a release and is not a random per-request
cache buster.

## V2.1 regression recovery and DOM contract

V2 was not browser-safe because it kept the V1 static URLs while removing DOM
elements required by V1. Cached V1 JavaScript first failed while binding the
removed `[data-neighborhood]` control; once that was bridged, its
`updateSummary()` reproduced `Cannot set properties of null (setting
'textContent')` on `[data-generated-at]`. Cached V1 CSS also restored the
radial dot grid.

V2.1 restores an explicit Neighborhood toolbar action and treats
`#knowledge-graph-canvas`, the visible counts, status message, and graph shell
as the startup-critical contract. It validates these before API work. Optional
summary, analytics, tooltip, component, and panel elements are independently
guarded and cannot prevent a valid graph response from reaching Cytoscape.
Core phases are recorded on `window.__kgExplorerState` for local development;
concise phase diagnostics are logged only on `localhost`/`127.0.0.1`.

The dot grid is removed at its real source. Browser acceptance asserts that
computed `background-image` is `none` on the graph canvas, its `::before` and
`::after` pseudo-elements, and its parent container.

## V2.2 semantic node roles

`DOCUMENT` and `DEPARTMENT` remain the only persistent node types. Candidate
and unresolved targets remain `kg_reference_candidates` relation/evidence
state and are not fabricated as graph nodes. Each document's visual role is
recalculated on every graph response from the already-loaded, trusted
`REFERENCES` relations only:

- `ISOLATED_DOCUMENT`: no incoming or outgoing `REFERENCES`;
- `REFERENCED_DOCUMENT`: incoming but no outgoing `REFERENCES`;
- `CITING_DOCUMENT`: outgoing but no incoming `REFERENCES`;
- `REFERENCED_AND_CITING_DOCUMENT`: both incoming and outgoing `REFERENCES`.

The graph uses color and shape/border together: neutral dashed ellipses for
isolated documents, blue rounded rectangles for referenced documents, amber
hexagons for citing documents, violet double-border octagons for both roles,
and teal diamonds for departments. The legend explains the same contract.
Tooltip and inspector views show the graph role and incoming, outgoing, and
total validated `REFERENCES` counts. A refresh therefore updates the role
without hardcoded document identity or another database query. Selection adds
a gold border/underlay without replacing the role fill or shape.

## Browser acceptance

Playwright is development/test-only (`requirements-dev.txt`), not a production
dependency. With the Django server running against native PostgreSQL:

```powershell
$env:RUN_BROWSER_E2E='1'
$env:KG_E2E_BASE_URL='http://127.0.0.1:8000/knowledge-graph/'
python -m pytest -q tests/browser/test_knowledge_graph_explorer.py
```

The suite uses physical browser clicks and validates shared-sidebar state across
pages, semantic role styles and inspection, panel width reclamation,
all graph modes and neighborhood directions, search/filters/components,
node/edge provenance, refresh, local JSON/PNG exports, focus/fullscreen,
filtered and error states, a clean console, and 1366×768, 1440×900, and
1920×1080 viewports. It writes the named acceptance screenshots and structured
evidence under `outputs/kg_visualization_v22/`.

## API summary

- `GET /knowledge-graph/api/summary/`
- `GET /knowledge-graph/api/graph/`
- `GET /knowledge-graph/api/neighborhood/<document_id>/`
- `GET /knowledge-graph/api/search/`
- `GET /knowledge-graph/api/nodes/<node_id>/`
- `GET /knowledge-graph/api/edges/<relation_id>/`
- `GET /knowledge-graph/api/components/`

Use `scope=documents` or `scope=administrative` on graph and search endpoints.
Use `download=1` on the graph endpoint for a logical filtered JSON export; PNG
export is rendered entirely client-side by Cytoscape. Neither export is a
database backup. V2.1 root-cause evidence remains under
`outputs/kg_visualization_v21/`; V2.2 sidebar, semantic-node, browser, and
integrity evidence is under `outputs/kg_visualization_v22/`.
