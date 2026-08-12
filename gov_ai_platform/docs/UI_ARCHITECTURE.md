# UI architecture

## Product boundary

The Government Administrative AI Copilot is a server-rendered Django portal
over the controlled PostgreSQL corpus. It is evidence-first: search returns the
existing ranked `SearchResult` passages and never manufactures an answer. The
portal does not change BAAI/bge-m3, 1,024-dimensional embeddings, cosine SQL,
Top-K behavior, or frozen retrieval labels.

## Routes and workspaces

- `/` — chat-first evidence search with Top-K and department filters
- `/documents/` — bounded, paginated metadata library
- `/documents/<id>/` — Overview, Evidence, PDF, Relationships, Metadata tabs
- `/documents/<id>/pdf/` — path-confined inline source PDF
- `/knowledge-graph/` — verified graph and candidate summary
- `/knowledge-graph/unresolved/` — paginated missing-target groups
- `/knowledge-graph/unresolved/<candidate-id>/` — grouped source evidence
- `/system/corpus/` — live read-only health and stored diagnostics
- `/system/evaluation/` — frozen retrieval and parser artifacts

## Layers

```text
Django views
  -> RetrievalService (submitted semantic searches only)
  -> PortalRepository / PortalService (bounded read-only projections)
  -> GraphService / GraphRepository (graph provenance)
  -> PostgreSQL and fixed, versioned JSON artifacts
```

Views contain no SQL. `knowledge_engine.portal.PortalRepository` owns
parameterized filters, whitelisted sorting, pagination, and aggregates.
`PortalService` combines live counts with fixed artifacts under
`outputs/knowledge_graph_v1/`. GET requests outside submitted search do not
construct or load BGE-M3.

## Presentation and progressive enhancement

`templates/base.html` provides the shared three-panel shell. CSS implements
desktop, tablet, and mobile layouts without a frontend framework. On small
screens navigation and context become drawers; links and forms remain usable
without JavaScript. JavaScript only enhances drawer controls, source selection,
Ctrl/Cmd+Enter, and visible loading state.

Semantic headings, labels, visible text status, keyboard focus, reduced-motion
handling, wrapping, and PDF fallbacks are present in the HTML.

## Trust and safety

- Similarity means retrieval proximity, not confidence or a conclusion.
- Verified edges and candidates have different text labels as well as colors.
- Every excerpt retains document and page provenance.
- Unresolved means no target was found in this corpus; it does not establish
  that the referenced document does not exist.
- Evaluation states the 61-document and CODEX_SOURCE_REVIEW limits.
- Browser routes never receive or render raw filesystem paths.

## Validation contract

The Django suite covers legacy behavior, routes, validation, lazy model
construction, provenance, PDF routing, graph labels, disclaimers, and escaping.
Release validation also checks live counts, five searches, five document pages,
five PDFs, three relationship views, both system dashboards, the frozen
29-question benchmark, and desktop browser interaction.
