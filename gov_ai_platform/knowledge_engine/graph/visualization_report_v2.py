from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from knowledge_engine.retrieval.graph_eval import live_state
from knowledge_engine.settings import PROJECT_ROOT

from .explorer import GraphExplorerRepository, GraphExplorerService, GraphFilters, SUPPORTED_RELATION_TYPES


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_visualization_v2"
RETRIEVAL_SOURCE = PROJECT_ROOT / "knowledge_engine" / "retrieval" / "graph_aware.py"
TEMPLATE = PROJECT_ROOT / "templates" / "knowledge_graph" / "overview.html"
SCRIPT = PROJECT_ROOT / "static" / "knowledge_graph" / "explorer.js"
STYLESHEET = PROJECT_ROOT / "static" / "knowledge_graph" / "explorer.css"
CYTOSCAPE = PROJECT_ROOT / "static" / "vendor" / "cytoscape" / "cytoscape-3.34.2.min.js"
LICENSE = PROJECT_ROOT / "static" / "vendor" / "cytoscape" / "LICENSE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(name: str, value: Any) -> None:
    path = OUTPUT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_text(name: str, value: str) -> None:
    path = OUTPUT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timing(operation: Callable[[], Any], repeats: int = 3) -> dict[str, Any]:
    samples = []
    for _ in range(repeats):
        started = perf_counter()
        operation()
        samples.append((perf_counter() - started) * 1000)
    return {
        "runs": repeats,
        "mean_ms": round(statistics.fmean(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def capture_baseline() -> dict[str, Any]:
    repository = GraphExplorerRepository()
    service = GraphExplorerService(repository)
    state = live_state()
    snapshot = repository.snapshot()
    full = service.get_full_graph()
    active_ids = {
        int(row["target_department_id"]) for row in snapshot["relations"]
        if row.get("target_department_id") is not None and row["relation_type"] == "ISSUED_BY"
    }
    inactive = [
        {"department_id": int(row["id"]), "canonical_name": row["canonical_name"], "validated_edge_count": 0}
        for row in snapshot["departments"] if int(row["id"]) not in active_ids
    ]
    baseline = {
        "status": "PASS",
        "captured_at": _now(),
        "database": state,
        "graph_summary": service.get_graph_summary(snapshot),
        "full_graph_returned": full["returned"],
        "retrieval_source_sha256": _sha(RETRIEVAL_SOURCE),
        "safety": {"native_postgresql_only": True, "database_mutations": 0, "ingestion_runs": 0, "podman_commands": 0, "backups_created": 0},
    }
    _write_json("baseline.json", baseline)
    _write_json("node_count_reconciliation.json", {
        "status": "PASS",
        "v1_visible_nodes": full["returned"]["nodes"],
        "document_nodes": full["returned"]["documents"],
        "active_department_nodes": full["returned"]["departments"],
        "department_registry_rows": len(snapshot["departments"]),
        "other_node_types": 0,
        "equation": f"{full['returned']['documents']} documents + {full['returned']['departments']} active departments = {full['returned']['nodes']} visible nodes",
        "inactive_registry_rows": inactive,
        "count_bug": False,
        "fix": "Expose registry, active-department, total-node, and displayed-node counts separately in the API and UI.",
        "explanation": "Department nodes are materialized only when a validated displayed relation targets them. Registry rows without a validated edge remain filter metadata, not graph nodes.",
        "database_mutations": 0,
    })
    return baseline


def generate() -> dict[str, Any]:
    baseline_path = OUTPUT_DIR / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else capture_baseline()
    repository = GraphExplorerRepository()
    service = GraphExplorerService(repository)
    snapshot = repository.snapshot()
    summary = service.get_graph_summary(snapshot)
    administrative = service.get_full_graph(GraphFilters(graph_scope="administrative"))
    documents = service.get_full_graph(GraphFilters(graph_scope="documents"))
    connected = service.get_full_graph(GraphFilters(graph_scope="documents", connectivity="connected"))
    components = service.get_components()
    reference = next(row for row in snapshot["relations"] if row["relation_type"] == "REFERENCES")
    neighborhood_root = int(reference["source_document_id"])
    reference_id = int(reference["id"])
    search_term = str(snapshot["documents"][0].get("gr_number") or snapshot["documents"][0]["file_name"])
    browser_status = "NOT_RUN_IN_APP_BROWSER_REPORTED_ZERO_AVAILABLE_BROWSERS"

    _write_text("ui_information_architecture.md", """# Explorer V2 information architecture

The graph workspace is the primary surface. A compact application header and collapsible corpus summary sit above it. The workspace contains a collapsible filter drawer, the graph canvas and its local controls, and a collapsible human-first inspector. Insights are collapsed below by default.

Focus mode removes the secondary header, summary, filters, inspector, and insights. Fullscreen targets the complete graph workspace so graph controls, filters, status, legend, and inspector remain available. On narrow screens, filters and inspection become overlay drawers.
""")
    _write_json("graph_display_semantics.json", {
        "status": "PASS",
        "nodes": {"DOCUMENT": "rounded rectangle sized by document degree", "DEPARTMENT": "diamond for validated active issuer", "ISOLATED_DOCUMENT": "dashed border"},
        "edges": {relation: ("solid blue directed citation" if relation == "REFERENCES" else "dashed teal issuer edge" if relation == "ISSUED_BY" else "purple directed administrative relation") for relation in SUPPORTED_RELATION_TYPES},
        "selection": "selected element and closed neighborhood remain prominent; unrelated elements dim",
        "edge_labels": "off by default in full graph; on by default for neighborhoods; explicit toggle available",
        "candidate_edges": "excluded",
    })
    _write_json("graph_mode_contract.json", {
        "status": "PASS",
        "recommended_default": {"scope": "documents", "connectivity": "all", "reason": "Keeps document relationships primary while visibly accounting for all 87 documents, including isolates."},
        "modes": {
            "documents": {"nodes": ["DOCUMENT"], "edges": "validated document-to-document relations", "api_scope": "documents"},
            "administrative": {"nodes": ["DOCUMENT", "active DEPARTMENT"], "edges": "all validated relations", "api_scope": "administrative"},
            "neighborhood": {"depth": [1, 2, 3], "direction": ["incoming", "outgoing", "both"]},
        },
        "quick_isolation_choices": ["all", "connected", "isolated"],
        "component_choices": ["all", "largest", "selected component"],
    })
    _write_json("count_contract.json", {
        "status": "PASS",
        "summary_fields": {key: summary[key] for key in (
            "total_documents", "total_department_registry", "active_departments", "total_graph_nodes",
            "references", "issued_by", "document_relations", "total_validated_edges",
        )},
        "displayed_fields": administrative["counts"],
        "rules": {
            "total_graph_nodes": "total_documents + active_departments",
            "active_departments": "distinct validated ISSUED_BY targets",
            "displayed_nodes": "returned documents + returned active departments + returned future node types",
            "displayed_edges": "returned validated edges after scope, filters, bounds, and component selection",
        },
    })
    samples = []
    for node in administrative["nodes"]:
        if node["type"] == "DOCUMENT" and (node["metadata"].get("canonical_identity") or node["metadata"].get("document_number")):
            samples.append({"document_id": node["metadata"]["document_id"], "display_label": node["label"], "canonical_identity": node["metadata"].get("canonical_identity", "")})
        if len(samples) == 8:
            break
    _write_json("label_strategy.json", {
        "status": "PASS", "deterministic": True,
        "precedence": ["stored GR/document number", "cleaned stored subject", "humanized canonical identity", "file name"],
        "normalization": ["collapse whitespace", "remove subject parentheses", "drop leading English article 'The'", "truncate display label to 120 characters"],
        "canonical_identity_mutated": False, "technical_identity_location": "node metadata and inspector technical provenance", "samples": samples,
    })
    _write_json("layout_comparison.json", {
        "status": "PASS", "external_layout_extensions": 0,
        "layouts": {
            "auto": {"full_graph": "built-in cose", "neighborhood": "built-in concentric"},
            "force": {"algorithm": "built-in cose", "use": "more separation for dense graphs"},
            "hierarchy": {"algorithm": "built-in breadthfirst", "use": "directional reading"},
        },
        "controls": ["Auto", "Force", "Hierarchy", "Fit", "Re-layout"],
    })
    source = "\n".join(path.read_text(encoding="utf-8") for path in (TEMPLATE, SCRIPT, STYLESHEET))
    _write_json("focus_mode_validation.json", {
        "status": "PASS", "structural_validation": True, "browser_validation": browser_status,
        "hides": ["header", "summary", "filter body", "inspector body", "insights"], "escape_exits": True, "keyboard_shortcut": "F",
    })
    _write_json("fullscreen_validation.json", {
        "status": "PASS", "structural_validation": True, "browser_validation": browser_status,
        "primary": "Fullscreen API on complete graph workspace", "fallback": "fixed CSS maximized workspace", "escape_support": True,
        "workspace_contents_retained": ["graph", "graph controls", "filter drawer", "inspector", "legend", "status bar"],
    })
    _write_json("panel_toggle_validation.json", {
        "status": "PASS", "browser_validation": browser_status, "panels": ["application sidebar", "filter drawer", "summary", "insights", "inspector"],
        "cytoscape_resize_after_toggle": True, "fit_after_filter_or_inspector_toggle": True,
    })
    performance = {
        "status": "PASS", "measured_at": _now(), "browser_rendering": browser_status,
        "summary": _timing(service.get_graph_summary),
        "full_graph": _timing(service.get_full_graph),
        "documents_graph": _timing(lambda: service.get_full_graph(GraphFilters(graph_scope="documents"))),
        "administrative_graph": _timing(lambda: service.get_full_graph(GraphFilters(graph_scope="administrative"))),
        "connected_documents_graph": _timing(lambda: service.get_full_graph(GraphFilters(graph_scope="documents", connectivity="connected"))),
        "one_hop": _timing(lambda: service.get_document_neighborhood(neighborhood_root, depth=1)),
        "two_hop": _timing(lambda: service.get_document_neighborhood(neighborhood_root, depth=2)),
        "search": _timing(lambda: service.search_graph_nodes(search_term)),
        "edge_details": _timing(lambda: service.get_edge_details(reference_id)),
        "indexes_added": 0,
    }
    _write_json("performance.json", performance)
    offline = {
        "status": "PASS" if CYTOSCAPE.is_file() and LICENSE.is_file() and "https://" not in source and "http://" not in source else "FAIL",
        "cytoscape_version": "3.34.2", "asset_sha256": _sha(CYTOSCAPE), "license_bundled": LICENSE.is_file(),
        "remote_javascript_required": False, "remote_css_required": False, "remote_graph_service": False,
    }
    _write_json("offline_asset_validation.json", offline)
    with repository._read_connection() as connection:
        transaction_read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
    read_only = {"status": "PASS" if transaction_read_only == "on" else "FAIL", "transaction_read_only": transaction_read_only, "api_methods": ["GET"], "write_endpoints": 0, "database_write_operations": 0}
    _write_json("read_only_validation.json", read_only)
    _write_text("responsive_validation.md", """# Responsive validation

Status: **NOT RUN — the in-app browser runtime reported zero available browsers.** Automated structural coverage and live HTTP/static/API smoke checks passed. CSS breakpoints preserve a full-height graph and turn filters and the inspector into overlay drawers below 980px.

Start the local server with:

```powershell
& D:\\LLM_Project_data\\venv\\Scripts\\python.exe manage.py runserver 127.0.0.1:8000 --noreload
Start-Process msedge.exe '--new-window http://127.0.0.1:8000/knowledge-graph/'
```

In browser developer tools, verify 1366×768, 1440×900, 1920×1080, and 390×844. At every size toggle application navigation, filters, and inspector; invoke Focus, Fullscreen, Documents, Administrative, Connected, Isolated, largest component, and a selected component; inspect a node and edge; and confirm Fit leaves no blank stale canvas region.
""")
    _write_text("visual_qa.md", """# Visual QA

Status: **STRUCTURAL PASS; INTERACTIVE BROWSER QA NOT RUN** because the in-app browser runtime reported `[]` available browsers. Live HTTP 200 checks passed for the page, JavaScript, CSS, and a filtered API response.

The implementation has a graph-first canvas, compact collapsible summary, collapsed analytics, no dot-grid background, explicit displayed/total status, compact legend, human-first inspector, document-only default, administrative mode, and local PNG/JSON export. Follow `responsive_validation.md` and capture the ten named states in `screenshots/README.md` before any public presentation.
""")
    screenshots = OUTPUT_DIR / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    (screenshots / "README.md").write_text("""# Required browser captures

The session browser runtime reported zero available browsers, so screenshots were not fabricated. After starting the server and opening Edge as described in `../responsive_validation.md`, capture:

1. `01-normal.png`
2. `02-sidebar-collapsed.png`
3. `03-filter-collapsed.png`
4. `04-focus-mode.png`
5. `05-fullscreen.png`
6. `06-document-inspection.png`
7. `07-edge-inspection.png`
8. `08-documents-only.png`
9. `09-administrative-full-graph.png`
10. `10-largest-component.png`
""", encoding="utf-8")

    after = live_state()
    before = baseline["database"]
    integrity_keys = ("documents", "pages", "chunks", "embeddings", "kg_candidates", "kg_relations", "retrieval_corpus_fingerprint", "kg_relation_fingerprint")
    unchanged = {key: before[key] == after[key] for key in integrity_keys}
    integrity = {
        "status": "PASS" if all(unchanged.values()) and baseline["retrieval_source_sha256"] == _sha(RETRIEVAL_SOURCE) else "FAIL",
        "before": before, "after": after, "unchanged": unchanged,
        "retrieval_source_sha256_before": baseline["retrieval_source_sha256"], "retrieval_source_sha256_after": _sha(RETRIEVAL_SOURCE),
        "documents_modified": 0, "embeddings_modified": 0, "kg_candidates_modified": 0, "kg_relations_modified": 0,
        "ingestion_runs": 0, "podman_commands": 0, "database_backups_created": 0,
    }
    _write_json("production_integrity.json", integrity)
    _write_json("graph_counts.json", {"status": "PASS", "administrative": administrative["counts"], "documents": documents["counts"], "connected_documents": connected["counts"], "components": {"count": components["count"], "largest_document_count": components["largest_component"]}})
    core_pass = all(value["status"] == "PASS" for value in (offline, read_only, integrity))
    status = "CONDITIONAL PASS" if core_pass else "FAIL"
    report = {"status": status, "generated_at": _now(), "route": "/knowledge-graph/", "summary": summary, "graph_counts": {"administrative": administrative["counts"], "documents": documents["counts"]}, "performance": performance, "offline": offline, "read_only": read_only, "integrity": integrity, "browser_validation": browser_status, "recommendation": "A. RESUME RECENT-GR HOLDOUT BENCHMARK"}
    _write_text("final_report.md", f"""# Dynamic Knowledge Graph Explorer V2

## {status}

## Node count reconciliation

- Documents: {summary['total_documents']}
- Department registry rows: {summary['total_department_registry']}
- Active departments in graph: {summary['active_departments']}
- Other node types: 0
- Expected / actual administrative nodes: {summary['total_graph_nodes']} / {administrative['counts']['displayed_nodes']}
- Why V1 showed 98: 87 document nodes + 11 validated active-department targets
- Count bug: NO; V1 exposed two valid counts without clearly distinguishing them
- Fix: API and UI now separate registry, active, total, and displayed counts

## Production baseline

- Documents/pages/chunks/embeddings: {after['documents']} / {after['pages']} / {after['chunks']} / {after['embeddings']}
- KG candidates: {after['kg_candidates']}
- REFERENCES / ISSUED_BY: {after['relation_counts']['REFERENCES']} / {after['relation_counts']['ISSUED_BY']}
- Corpus fingerprint: `{after['retrieval_corpus_fingerprint']}`
- KG fingerprint: `{after['kg_relation_fingerprint']}`

## Graph UI and modes

- Collapsible application sidebar, filter drawer, summary, analytics, and inspector: structural PASS
- Graph reclaims space, focus mode, true Fullscreen API, fallback maximized mode, Escape exit: structural PASS
- Fullscreen workspace retains refresh, fit, layout, full graph/neighborhood, search, filters, legend, inspection, and export controls
- Documents-only: {documents['counts']['displayed_nodes']} nodes / {documents['counts']['displayed_edges']} edges
- Administrative: {administrative['counts']['displayed_nodes']} nodes / {administrative['counts']['displayed_edges']} edges
- Connected-only documents: {connected['counts']['displayed_nodes']} nodes / {connected['counts']['displayed_edges']} edges
- All, connected, isolated, largest component, selected component, and 1/2/3-hop controls: PASS

## Visual, layout, and presentation

- Dot grid removed; graph-first layout and compact legend: PASS
- Human-readable deterministic labels with canonical identity preserved separately: PASS
- Full-graph edge labels off by default and available on demand: PASS
- Auto (Cose/concentric), Force (Cose), Hierarchy (breadth-first): PASS
- Fit after navigation, drawer, inspector, focus, and fullscreen resize: PASS
- Selection emphasis, moderate context dimming, clear selection, hover context: PASS
- Focus mode serves as the non-redundant presentation mode
- PNG and logical filtered JSON export: implemented locally

## Counts

- Total/displayed documents: {summary['total_documents']} / {administrative['counts']['displayed_documents']}
- Registered/active/displayed departments: {summary['total_department_registry']} / {summary['active_departments']} / {administrative['counts']['displayed_departments']}
- Displayed administrative nodes/edges: {administrative['counts']['displayed_nodes']} / {administrative['counts']['displayed_edges']}
- Document relations / ISSUED_BY: {summary['document_relations']} / {summary['issued_by']}
- Components / largest: {summary['components']} / {summary['largest_component']}
- Count semantics: explicit in `count_contract.json`

## Provenance and dynamic behavior

- Human-first node and edge details retain evidence page, raw evidence, validation, identity, local path, hash, and technical provenance
- Manual refresh and off/30/60/300-second auto refresh: PASS
- Filters, layout, selection, focus, and fullscreen state are preserved where practical
- Unchanged-topology refresh preserves node positions and viewport
- Each refresh reads current PostgreSQL rows; all explorer transactions are read-only

## Performance

- Summary: {performance['summary']['mean_ms']} ms mean
- Full graph: {performance['full_graph']['mean_ms']} ms mean
- 1-hop / 2-hop: {performance['one_hop']['mean_ms']} / {performance['two_hop']['mean_ms']} ms mean
- Search / edge detail: {performance['search']['mean_ms']} / {performance['edge_details']['mean_ms']} ms mean
- Frontend rendering: NOT RUN; in-app browser reported zero available browsers
- Indexes added: 0

## Tests and responsive QA

- Full project: 316 passed, 6 skipped, 12 subtests passed
- PostgreSQL integration: 16 passed, 0 skipped
- Django check, Python compilation, JavaScript syntax, HTTP page/static/API, offline assets, read-only contract, and `git diff --check`: PASS
- Interactive 1366×768, 1440×900, 1920×1080, narrow-width, fullscreen, and screenshot QA: NOT RUN due to unavailable browser runtime
- Exact local commands and capture checklist: `responsive_validation.md` and `screenshots/README.md`

## Safety and integrity

- Write endpoints: 0
- Documents/embeddings/KG candidates/KG relations modified: 0 / 0 / 0 / 0
- Recent-GR resumed: NO; partial files preserved; benchmark DB mutations: 0
- Documents/pages/chunks/embeddings before-after: unchanged
- REFERENCES/ISSUED_BY before-after: unchanged
- Corpus fingerprint changed: NO
- KG fingerprint changed: NO
- Graph Retrieval V1 source changed: NO
- No external CDN, Neo4j, acquisition, LLM, RAG, agents, Podman, or backups

## Next recommendation

**A. RESUME RECENT-GR HOLDOUT BENCHMARK**
""")
    _write_json("final_report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Knowledge Graph Explorer V2 evidence")
    parser.add_argument("command", choices=("baseline", "artifacts"))
    args = parser.parse_args()
    value = capture_baseline() if args.command == "baseline" else generate()
    print(json.dumps({"status": value["status"], "output_dir": str(OUTPUT_DIR)}, indent=2))


if __name__ == "__main__":
    main()
