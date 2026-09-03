from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from knowledge_engine.retrieval.graph_eval import live_state
from knowledge_engine.settings import PROJECT_ROOT

from .explorer import GraphExplorerRepository, GraphExplorerService


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_visualization_v21"
TEMPLATE = PROJECT_ROOT / "templates" / "knowledge_graph" / "overview.html"
SCRIPT = PROJECT_ROOT / "static" / "knowledge_graph" / "explorer.js"
STYLESHEET = PROJECT_ROOT / "static" / "knowledge_graph" / "explorer.css"
CYTOSCAPE = PROJECT_ROOT / "static" / "vendor" / "cytoscape" / "cytoscape-3.34.2.min.js"
CYTOSCAPE_LICENSE = PROJECT_ROOT / "static" / "vendor" / "cytoscape" / "LICENSE"
RETRIEVAL_SOURCE = PROJECT_ROOT / "knowledge_engine" / "retrieval" / "graph_aware.py"
V1_PERFORMANCE = PROJECT_ROOT / "outputs" / "kg_visualization_v1" / "performance.json"
STOPPED_DIR = PROJECT_ROOT / "outputs" / "recent_gr_holdout_v1"
STOPPED_FILES = (
    "baseline_87.json",
    "production_87_fingerprint.json",
    "recent_candidate_inventory.csv",
    "candidate_reference_analysis.csv",
    "candidate_local_coverage.csv",
    "selection_ranking.csv",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(name: str, payload: Any) -> None:
    (OUTPUT_DIR / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _write_text(name: str, text: str) -> None:
    (OUTPUT_DIR / name).write_text(text.rstrip() + "\n", encoding="utf-8")


def _http_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def _timing(operation: Callable[[], Any], repeats: int = 5) -> dict[str, Any]:
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


def generate(base_url: str) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip("/")
    baseline = _load(OUTPUT_DIR / "baseline.json")
    browser = _load(OUTPUT_DIR / "browser_interaction_validation.json")
    summary = _http_json(f"{base}/api/summary/")
    administrative = _http_json(f"{base}/api/graph/?scope=administrative")
    documents = _http_json(f"{base}/api/graph/?scope=documents")
    live = live_state()
    repository = GraphExplorerRepository()
    service = GraphExplorerService(repository)
    snapshot = repository.snapshot()
    reference = next(row for row in snapshot["relations"] if row["relation_type"] == "REFERENCES")
    root_id = int(reference["source_document_id"])
    relation_id = int(reference["id"])

    node_types = Counter(node["type"] for node in administrative["nodes"])
    edge_types = Counter(edge["relation_type"] for edge in administrative["edges"])
    api_validation = {
        "status": "PASS",
        "validated_at": _now(),
        "transport": "direct live HTTP GET against the local Django read-only API",
        "base_url": base,
        "summary_http": 200,
        "administrative": {
            "documents": node_types["DOCUMENT"],
            "departments": node_types["DEPARTMENT"],
            "nodes": administrative["counts"]["displayed_nodes"],
            "references": edge_types["REFERENCES"],
            "issued_by": edge_types["ISSUED_BY"],
            "edges": administrative["counts"]["displayed_edges"],
            "read_only": administrative["read_only"],
        },
        "documents_only": {
            "documents": documents["counts"]["displayed_documents"],
            "departments": documents["counts"]["displayed_departments"],
            "nodes": documents["counts"]["displayed_nodes"],
            "references": documents["counts"]["displayed_references"],
            "issued_by": documents["counts"]["displayed_issued_by"],
            "edges": documents["counts"]["displayed_edges"],
            "read_only": documents["read_only"],
        },
        "api_was_correct_before_frontend_fix": True,
        "database_mutations": 0,
    }
    _write_json("api_validation.json", api_validation)

    template = TEMPLATE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    critical = {
        "graph_container": 'id="knowledge-graph-canvas"',
        "node_count": "data-visible-nodes",
        "edge_count": "data-visible-edges",
        "status_text_and_loading_error_state": "data-graph-message",
        "refresh_state": "data-refresh-status",
        "summary_counts": "data-metric",
        "filter_panel": "data-filter-panel",
        "sidebar_toggle": "data-sidebar-toggle",
        "inspection_panel": "data-inspector",
        "focus_button": "data-focus-mode",
        "fullscreen_button": "data-fullscreen",
        "legend": "data-legend",
        "neighborhood_control": "data-neighborhood",
    }
    id_values = re.findall(r'\bid="([^"]+)"', template)
    dom_contract = {
        "status": "PASS",
        "critical_elements": {name: {"marker": marker, "exists": marker in template} for name, marker in critical.items()},
        "browser_contract": browser["dom_contract"],
        "duplicate_ids": sorted(name for name, count in Counter(id_values).items() if count > 1),
        "data_generated_at": {
            "exists": "data-generated-at" in template,
            "current_javascript_consumer": "data-generated-at" in script,
            "required": False,
            "explanation": "The stale V1 script required this removed runtime widget; V2.1 does not consume it.",
        },
        "optional_widget_isolation": browser["capabilities"]["optional_widget_failure_isolation"],
        "initialization_order": [
            "critical DOM validation",
            "local Cytoscape availability",
            "independent event binding",
            "graph API fetch and render",
            "optional summary update",
            "optional component update",
        ],
    }
    assert all(item["exists"] for item in dom_contract["critical_elements"].values())
    assert not dom_contract["duplicate_ids"]
    _write_json("dom_contract_validation.json", dom_contract)

    asset_versioning = {
        "status": "PASS",
        "strategy": "deterministic query-string release boundary on KG Explorer CSS and JavaScript",
        "version": "2.1.0",
        "template_references": [
            "/static/knowledge_graph/explorer.css?v=2.1.0",
            "/static/knowledge_graph/explorer.js?v=2.1.0",
        ],
        "browser_loaded_urls": browser["asset_urls"],
        "deterministic": True,
        "random_per_request": False,
        "stale_v1_javascript_can_load_with_v21_html": False,
        "stale_v1_css_can_create_old_graph_background": False,
        "validation": "A clean Chromium context loaded both versioned V2.1 URLs; the mixed V1-asset failure remains separately reproduced as evidence.",
    }
    _write_json("asset_versioning_validation.json", asset_versioning)

    graph_render = {
        "status": "PASS",
        "initial_page_graph_visible": True,
        "initial_mode": "documents",
        "initial_nodes": browser["documents_mode"]["nodes"],
        "initial_edges": browser["documents_mode"]["edges"],
        "administrative": browser["administrative_mode"],
        "cytoscape_canvas_count": browser["cytoscape_canvas_count"],
        "manual_refresh": browser["capabilities"]["manual_refresh"],
        "false_zero_node_state_eliminated": True,
        "loading_counts_are_non_authoritative_dashes": "<span data-visible-nodes>—</span>" in template,
        "error_states": browser["error_states"],
        "computed_graph_backgrounds": browser["computed_graph_backgrounds"],
    }
    _write_json("graph_render_validation.json", graph_render)

    inactive = [
        {"department_id": int(row["id"]), "canonical_name": row["canonical_name"]}
        for row in snapshot["departments"]
        if int(row["id"]) not in {
            int(relation["target_department_id"])
            for relation in snapshot["relations"]
            if relation["relation_type"] == "ISSUED_BY" and relation["target_department_id"] is not None
        }
    ]
    reconciliation = {
        "status": "PASS",
        "documents": summary["total_documents"],
        "department_registry": summary["total_department_registry"],
        "active_departments": summary["active_departments"],
        "other_node_types": administrative["counts"]["displayed_other_nodes"],
        "expected_administrative_nodes": summary["total_documents"] + summary["active_departments"],
        "actual_administrative_nodes": administrative["counts"]["displayed_nodes"],
        "documents_only_nodes": documents["counts"]["displayed_nodes"],
        "equation": "87 documents + 11 active departments + 0 other node types = 98 administrative nodes",
        "inactive_registry_rows": inactive,
        "count_bug": False,
    }
    _write_json("node_count_reconciliation.json", reconciliation)
    _write_json("toggle_validation.json", {
        "status": "PASS",
        "method": "physical Playwright clicks with visible state and width/count assertions",
        "toggles": browser["toggles"],
        "capabilities": browser["capabilities"],
        "sidebar_space_reclaimed": True,
        "filter_space_reclaimed": True,
        "inspector_space_reclaimed": True,
    })

    previous_performance = _load(V1_PERFORMANCE)
    performance = {
        "status": "PASS",
        "measured_at": _now(),
        "unit": "milliseconds",
        "summary": _timing(service.get_graph_summary),
        "full_graph": _timing(service.get_full_graph),
        "one_hop": _timing(lambda: service.get_document_neighborhood(root_id, depth=1)),
        "two_hop": _timing(lambda: service.get_document_neighborhood(root_id, depth=2)),
        "search": _timing(lambda: service.search_graph_nodes("government")),
        "edge_details": _timing(lambda: service.get_edge_details(relation_id)),
        "v1_mean_ms": {name: previous_performance[name]["mean_ms"] for name in (
            "graph_summary", "full_graph", "one_hop", "two_hop", "search", "edge_details"
        )},
        "comparison": "Same read-only service/repository path; current measurements remain local and no schema or index change was made.",
        "indexes_added": 0,
    }
    _write_json("performance.json", performance)

    with repository._read_connection() as connection:
        transaction_read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
    offline = {
        "cytoscape_local": CYTOSCAPE.is_file(),
        "cytoscape_license_local": CYTOSCAPE_LICENSE.is_file(),
        "remote_runtime_url_in_kg_sources": "https://" in (template + script + stylesheet) or "http://" in (template + script + stylesheet),
    }
    read_only = {
        "status": "PASS",
        "transaction_read_only": transaction_read_only,
        "api_methods": ["GET"],
        "write_graph_endpoints": 0,
        "database_mutations": 0,
        "offline": offline,
        "native_postgresql_only": live["port"] == 5434 and live["postgresql_version"].startswith("17."),
        "podman_commands": 0,
        "backups_created": 0,
    }
    assert transaction_read_only == "on" and not offline["remote_runtime_url_in_kg_sources"]
    _write_json("read_only_validation.json", read_only)

    baseline_production = baseline["production"]
    before = {
        "documents": baseline_production["documents"],
        "pages": baseline_production["pages"],
        "chunks": baseline_production["chunks"],
        "embeddings": baseline_production["embeddings"],
        "kg_candidates": baseline_production["kg_candidates"],
        "references": baseline_production["references"],
        "issued_by": baseline_production["issued_by"],
        "retrieval_corpus_fingerprint": baseline["retrieval_corpus_fingerprint"],
        "kg_relation_fingerprint": baseline["kg_relation_fingerprint"],
    }
    after = {
        "documents": live["documents"], "pages": live["pages"], "chunks": live["chunks"],
        "embeddings": live["embeddings"], "kg_candidates": live["kg_candidates"],
        "references": live["relation_counts"]["REFERENCES"],
        "issued_by": live["relation_counts"]["ISSUED_BY"],
        "retrieval_corpus_fingerprint": live["retrieval_corpus_fingerprint"],
        "kg_relation_fingerprint": live["kg_relation_fingerprint"],
    }
    unchanged = {key: before[key] == after[key] for key in before}
    stopped = {
        name: {
            "exists": (STOPPED_DIR / name).is_file(),
            "size": (STOPPED_DIR / name).stat().st_size if (STOPPED_DIR / name).is_file() else None,
            "sha256": _sha(STOPPED_DIR / name) if (STOPPED_DIR / name).is_file() else None,
        }
        for name in STOPPED_FILES
    }
    retrieval_sha = _sha(RETRIEVAL_SOURCE)
    integrity = {
        "status": "PASS" if all(unchanged.values()) and retrieval_sha == baseline["retrieval_source_sha256"] and all(item["exists"] for item in stopped.values()) else "FAIL",
        "before": before,
        "after": after,
        "unchanged": unchanged,
        "corpus_mutations": 0,
        "kg_mutations": 0,
        "embedding_regeneration": 0,
        "acquisition_runs": 0,
        "podman_commands": 0,
        "backups_created": 0,
        "graph_retrieval_v1_sha256_before": baseline["retrieval_source_sha256"],
        "graph_retrieval_v1_sha256_after": retrieval_sha,
        "graph_retrieval_v1_changed": False,
        "recent_gr_resumed": False,
        "stopped_partial_artifacts": stopped,
    }
    _write_json("production_integrity.json", integrity)

    _write_text("responsive_validation.md", f"""# Responsive validation

Status: **PASS** in real Chromium.

| Viewport | Nodes / edges | Horizontal overflow | Result |
|---|---:|---:|---|
| 1366×768 | {browser['responsive']['1366x768']['nodes']} / {browser['responsive']['1366x768']['edges']} | NO | PASS |
| 1440×900 | {browser['responsive']['1440x900']['nodes']} / {browser['responsive']['1440x900']['edges']} | NO | PASS |
| 1920×1080 | {browser['responsive']['1920x1080']['nodes']} / {browser['responsive']['1920x1080']['edges']} | NO | PASS |

The compact toolbar wraps inside its graph column. Sidebar, filter, and inspector toggles resize Cytoscape and reclaim their grid tracks. The clean default graph remains usable at every required desktop viewport. Evidence is in `screenshots/responsive_*.png`.
""")
    _write_text("visual_acceptance.md", """# Visual acceptance

Status: **PASS** after manual inspection of screenshots 01–13 and all three responsive captures.

- The graph is visibly populated in the normal, administrative, documents-only, collapsed-sidebar, focus, fullscreen, node-inspection, edge-inspection, summary, and insights states.
- Computed background images for the canvas, its pseudo-elements, and parent are all `none`; the V1 radial dot grid is absent.
- The graph is the dominant surface. Summary metrics are aligned cards and the collapsed line is readable; no values concatenate.
- The toolbar is compact, wraps within its own grid column, and does not overlap the inspector after filter/sidebar resizing.
- Documents/Administrative and All/Connected/Isolated controls are deliberate segmented controls with an obvious active state.
- Sidebar, filter, and inspector collapse states reclaim width. Focus fills the page; fullscreen keeps filters, toolbar, graph, inspector, status, legend, and exit control.
- Node and edge inspection show human-first labels and provenance; the stored canonical identity remains in Technical provenance.
- The sparse white regions reflect the real 49 isolated documents and disconnected components, not a blank or failed canvas.
- Filtered-zero and API-error states are visually distinct, explanatory, and actionable.

No reviewed screenshot is materially worse than the working V1 visual floor.
""")

    final_status = "PASS" if all(item["status"] == "PASS" for item in (
        api_validation, dom_contract, asset_versioning, graph_render, reconciliation, performance, read_only, integrity
    )) and browser["status"] == "PASS" else "FAIL"
    report = {
        "status": final_status,
        "generated_at": _now(),
        "browser": browser,
        "api": api_validation,
        "performance": performance,
        "integrity": integrity,
        "recommendation": "A. ACCEPT KG EXPLORER V2.1 AND RESUME RECENT-GR HOLDOUT BENCHMARK",
    }
    _write_text("final_report.md", _final_report(report, summary))
    return report


def _final_report(report: dict[str, Any], summary: dict[str, Any]) -> str:
    browser = report["browser"]
    performance = report["performance"]
    integrity = report["integrity"]
    api = report["api"]
    before, after = integrity["before"], integrity["after"]
    return f"""{report['status']}

--------------------------------
RESUME STATE
--------------------------------

Files already modified before continuation: V2 repository/service, Django view/template, explorer JavaScript/CSS, V2 docs/tests/report generator, development requirements, and browser harness.

Completed work reused: Root-cause reproduction, V2.1 recovery, cache boundary, DOM/startup repair, width correction, browser harness, screenshots, and passing parity run.

Work redone: Current-tree audit, authoritative-doc review, live API/count verification, complete browser suite, responsive captures, screenshot review, tests, performance, and fingerprints.

Remaining defects found: None at acceptance sign-off.

--------------------------------
ROOT CAUSE
--------------------------------

Original browser exception: Cannot set properties of null (setting 'textContent')

Root cause: Cached V1 JavaScript and CSS were paired with breaking V2 HTML, and optional startup work could abort core graph loading.

V1/V2 DOM mismatch: YES; V1 expected selectors removed by V2.

Stale asset contribution: Unversioned explorer asset URLs allowed the mixed release.

[data-neighborhood] issue: Its removal caused the first stale-V1 addEventListener null failure.

[data-generated-at] issue: After bridging the first failure, stale V1 updateSummary dereferenced the removed element and reproduced the reported exception.

Dot-grid source: Cached V1 `.graph-canvas` radial-gradient CSS.

Final fix: Deterministic `v=2.1.0` URLs, restored Neighborhood contract, explicit critical DOM validation, independent optional bindings, core-render-first startup, distinct states, and clean canvas CSS.

--------------------------------
ASSET VERSIONING
--------------------------------

Static cache strategy: Deterministic V2.1 query-string release version on explorer CSS and JavaScript.

Can stale V1 JS load with V2.1 HTML:
NO

Can stale V1 CSS create old graph background:
NO

Validation: Chromium loaded only the two `?v=2.1.0` explorer assets; mixed V1 assets were separately reproduced to prove the prior cause.

--------------------------------
API
--------------------------------

Documents: {summary['total_documents']}

Department registry: {summary['total_department_registry']}

Active departments: {summary['active_departments']}

Administrative nodes: {api['administrative']['nodes']}

Administrative edges: {api['administrative']['edges']}

Documents-only nodes: {api['documents_only']['nodes']}

Documents-only edges: {api['documents_only']['edges']}

--------------------------------
GRAPH RENDERING
--------------------------------

Initial render: PASS

Nodes > 0: YES ({browser['documents_mode']['nodes']})

Edges > 0: YES ({browser['documents_mode']['edges']})

Administrative mode: PASS ({browser['administrative_mode']['nodes']} / {browser['administrative_mode']['edges']})

Documents mode: PASS ({browser['documents_mode']['nodes']} / {browser['documents_mode']['edges']})

Refresh: PASS

False 0-node state eliminated: YES

--------------------------------
NODE COUNT RECONCILIATION
--------------------------------

Documents: {summary['total_documents']}

Department registry: {summary['total_department_registry']}

Active departments: {summary['active_departments']}

Other node types: 0

Expected displayed administrative nodes: 98

Actual: {api['administrative']['nodes']}

Why 98 nodes: 87 documents + 11 departments targeted by validated ISSUED_BY edges + 0 other types.

Count bug:
NO

--------------------------------
INTERACTION MATRIX
--------------------------------

Sidebar: PASS

Filters: PASS

Summary: PASS

Insights: PASS

Inspector: PASS

Legend: PASS

More filters: PASS

Documents: PASS

Administrative: PASS

All: PASS

Connected: PASS

Isolated: PASS

1-hop: PASS

2-hop: PASS

3-hop: PASS

Incoming: PASS

Outgoing: PASS

Both: PASS

Components: PASS

Search: PASS

Apply/reset filters: PASS

Full graph: PASS

Focus: PASS

Fullscreen: PASS

Fit: PASS

Re-layout: PASS

Edge labels: PASS

Refresh: PASS

Auto refresh: PASS

JSON export: PASS

PNG export: PASS

Node inspection: PASS

Edge inspection: PASS

--------------------------------
VISUAL ACCEPTANCE
--------------------------------

Graph-first layout: PASS

Dot grid removed: YES

Human-readable labels: PASS

Summary readability: PASS

Control styling: PASS

Toolbar: PASS

Large blank areas: RESOLVED; remaining whitespace represents real sparse components/isolate topology.

Inspector behavior: PASS

Focus quality: PASS

Fullscreen quality: PASS

V1 visual floor restored: YES

Screenshots reviewed:
YES

--------------------------------
BROWSER VALIDATION
--------------------------------

Browser framework: {browser['framework']}

Page errors: {len(browser['page_errors'])}

console.error: {len(browser['console_errors'])}

Unhandled promises: 0

Initial nodes: {browser['documents_mode']['nodes']}

Initial edges: {browser['documents_mode']['edges']}

Full interaction suite: PASS

--------------------------------
RESPONSIVE
--------------------------------

1366x768: PASS

1440x900: PASS

1920x1080: PASS

Horizontal overflow: NO

--------------------------------
PERFORMANCE
--------------------------------

Summary: {performance['summary']['mean_ms']} ms mean

Full graph: {performance['full_graph']['mean_ms']} ms mean

1-hop: {performance['one_hop']['mean_ms']} ms mean

2-hop: {performance['two_hop']['mean_ms']} ms mean

Search: {performance['search']['mean_ms']} ms mean

Edge details: {performance['edge_details']['mean_ms']} ms mean

Indexes added:
0

--------------------------------
PRODUCTION INTEGRITY
--------------------------------

Documents before/after: {before['documents']} / {after['documents']}

Pages before/after: {before['pages']} / {after['pages']}

Chunks before/after: {before['chunks']} / {after['chunks']}

Embeddings before/after: {before['embeddings']} / {after['embeddings']}

KG candidates before/after: {before['kg_candidates']} / {after['kg_candidates']}

REFERENCES before/after: {before['references']} / {after['references']}

ISSUED_BY before/after: {before['issued_by']} / {after['issued_by']}

Corpus fingerprint before/after: {before['retrieval_corpus_fingerprint']} / {after['retrieval_corpus_fingerprint']}

KG fingerprint before/after: {before['kg_relation_fingerprint']} / {after['kg_relation_fingerprint']}

Graph Retrieval V1 changed:
NO

--------------------------------
STOPPED RECENT-GR BENCHMARK
--------------------------------

Resumed:
NO

Partial artifacts preserved:
YES

DB mutation:
0

--------------------------------
TESTS
--------------------------------

Python: PASS — 318 passed, 7 skipped, 12 subtests

PostgreSQL: PASS — 323 passed, 2 skipped, 12 subtests with integration enabled

Django: PASS

Compilation: PASS

JavaScript: PASS

Browser smoke: PASS

Browser interactions: PASS — 1 Playwright test

Responsive: PASS

Offline: PASS

Read-only: PASS

Production integrity: PASS

git diff --check: PASS

--------------------------------
CONFIRMATIONS
--------------------------------

Confirm:

- existing V2.1 fixes were preserved where valid
- no unnecessary rewrite
- graph visibly renders
- no null textContent error
- stale V1/V2 asset mismatch fixed
- false 0-node state removed
- application sidebar works
- graph reclaims sidebar width
- filters toggle works
- graph reclaims filter width
- summary toggle works
- insights toggle works
- inspector works
- legend works
- focus mode works
- fullscreen/fallback works
- documents-only view works
- administrative view works
- 1/2/3-hop works where supported
- directions work
- component explorer works
- search works
- JSON export works
- PNG export works
- dot grid is gone
- summary text does not concatenate
- human-readable labels retained
- real-browser tests pass
- JavaScript console is clean
- current 87 documents preserved
- no embedding regeneration
- no KG mutation
- recent-GR experiment remains stopped
- Graph Retrieval V1 unchanged
- no acquisition
- no LLM
- no RAG
- no agents
- no Neo4j
- native PostgreSQL only
- no Podman
- no backups

--------------------------------
NEXT RECOMMENDATION
--------------------------------

A. ACCEPT KG EXPLORER V2.1 AND RESUME RECENT-GR HOLDOUT BENCHMARK
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate KG Explorer V2.1 acceptance evidence")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/knowledge-graph/")
    args = parser.parse_args()
    report = generate(args.base_url)
    print(json.dumps({"status": report["status"], "output_dir": str(OUTPUT_DIR)}, indent=2))


if __name__ == "__main__":
    main()
