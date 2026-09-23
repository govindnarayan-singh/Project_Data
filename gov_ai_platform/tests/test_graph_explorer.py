from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase
from django.urls import reverse

from knowledge_engine.graph.explorer import (
    ExplorerValidationError,
    GraphExplorerRepository,
    GraphExplorerService,
    GraphFilters,
)
from tests.access_test_helpers import AuthorizedViewTestCase


def _document(document_id, *, kind, year, department, origin="LOCAL_CONTROLLED"):
    return {
        "id": document_id, "file_name": f"doc-{document_id}.pdf",
        "file_path": f"Government_Documents/External_Official/doc-{document_id}.pdf",
        "sha256": str(document_id) * 64, "department": department,
        "document_type": kind, "document_kind": kind, "gr_number": f"GR/{year}/{document_id}",
        "gr_date": date(year, 1, document_id), "subject": f"Policy document {document_id}",
        "page_count": document_id + 1, "digital_pages": document_id + 1, "ocr_pages": 0,
        "extraction_method": "digital", "source_origin": origin,
        "canonical_identity": f"gr:{year}:{document_id}", "document_year": year,
        "created_at": None,
    }


DOCUMENTS = [
    _document(1, kind="GOVERNMENT_RESOLUTION", year=2020, department="Finance"),
    _document(2, kind="GOVERNMENT_CIRCULAR", year=2021, department="Finance"),
    _document(3, kind="ACT", year=2022, department="Law", origin="EXTERNAL_OFFICIAL"),
    _document(4, kind="ORDER", year=2023, department="Law"),
]
DEPARTMENTS = [
    {"id": 10, "canonical_name": "Finance", "normalized_name": "finance", "aliases": ["FD"],
     "created_at": None, "updated_at": None},
    {"id": 11, "canonical_name": "Law", "normalized_name": "law", "aliases": ["Law Department"],
     "created_at": None, "updated_at": None},
]


def _relation(relation_id, source, relation_type, *, target=None, department=None,
              validation="AUTO_VERIFIED"):
    return {
        "id": relation_id, "source_document_id": source, "target_document_id": target,
        "target_department_id": department, "relation_type": relation_type,
        "validation_status": validation, "created_from_candidate_id": 900 + relation_id,
        "created_at": None, "updated_at": None, "source_file": f"doc-{source}.pdf",
        "source_number": f"GR/2020/{source}", "target_file": f"doc-{target}.pdf" if target else None,
        "target_number": f"GR/2020/{target}" if target else None,
        "target_department": {10: "Finance", 11: "Law"}.get(department),
        "evidence_count": 1 if target else 0, "evidence_page": 2 if target else None,
        "evidence_excerpt": "According to the earlier Government Resolution" if target else "",
        "resolution_state": "RESOLVED_EXACT" if target else None, "parser_version": "parser-v2",
    }


RELATIONS = [
    _relation(101, 1, "REFERENCES", target=2),
    _relation(102, 2, "REFERENCES", target=3, validation="HUMAN_CONFIRMED"),
    _relation(103, 1, "ISSUED_BY", department=10),
    _relation(104, 2, "ISSUED_BY", department=10),
    _relation(105, 3, "ISSUED_BY", department=11),
    _relation(106, 4, "ISSUED_BY", department=11),
    _relation(107, 3, "REFERENCES", target=1, validation="CANDIDATE"),
]


class FakeExplorerRepository:
    def __init__(self):
        self.state = {
            "runtime": {"postgresql_version": "17.11", "pgvector_version": "0.8.6",
                        "database_name": "grdb", "server_address": "127.0.0.1", "server_port": 5434},
            "documents": deepcopy(DOCUMENTS), "departments": deepcopy(DEPARTMENTS),
            "relations": deepcopy(RELATIONS),
        }

    def snapshot(self):
        return deepcopy(self.state)

    def document_details(self, document_id):
        row = next((deepcopy(item) for item in self.state["documents"] if item["id"] == document_id), None)
        if row:
            row.update({"chunk_count": 3, "external_candidate_id": None, "archive_source_root": None,
                        "archive_relative_path": None, "archived_sha256": None, "archived_at": None,
                        "source_authority": None, "source_adapter": None, "source_domain": None,
                        "source_url": None, "final_url": None})
        return row

    def department_details(self, department_id):
        row = next((deepcopy(item) for item in self.state["departments"] if item["id"] == department_id), None)
        if row:
            row["document_count"] = sum(
                item.get("target_department_id") == department_id
                and item["validation_status"] in {"AUTO_VERIFIED", "HUMAN_CONFIRMED"}
                for item in self.state["relations"]
            )
        return row

    def edge_details(self, relation_id):
        relation = next((item for item in self.state["relations"] if item["id"] == relation_id), None)
        if relation is None or relation["validation_status"] not in {"AUTO_VERIFIED", "HUMAN_CONFIRMED"}:
            return None
        value = deepcopy(relation)
        value.update({"source_subject": "Source subject", "source_origin": "LOCAL_CONTROLLED",
                      "source_path": "source.pdf", "source_sha256": "a" * 64,
                      "target_subject": "Target subject", "target_origin": "EXTERNAL_OFFICIAL",
                      "target_path": "target.pdf", "target_sha256": "b" * 64})
        value["evidence"] = [] if relation["relation_type"] == "ISSUED_BY" else [{
            "relation_evidence_id": 501, "evidence_link_created_at": None, "candidate_id": 901,
            "source_document_id": relation["source_document_id"], "source_page": 2,
            "reference_kind": "GOVERNMENT_RESOLUTION", "raw_evidence_text": "Stored source-page evidence",
            "raw_document_number": "GR/2021/2", "normalized_document_number": "gr/2021/2",
            "raw_reference_date": "", "normalized_reference_date": None, "raw_department": "Finance",
            "normalized_department": "finance", "pattern_id": "REF-GR", "parser_version": "parser-v2",
            "parser_status": "VALID", "parser_notes": "", "resolution_status": "RESOLVED_EXACT",
            "resolution_method": "EXACT_IDENTITY", "resolution_evidence": "one match",
            "confidence_category": "HIGH", "validation_status": "AUTO_VERIFIED",
            "evidence_hash": "e" * 64, "fingerprint": "f" * 64,
            "candidate_created_at": None, "candidate_updated_at": None,
        }]
        value["provenance_complete"] = True
        return value


class GraphExplorerServiceTests(SimpleTestCase):
    def setUp(self):
        self.repository = FakeExplorerRepository()
        self.service = GraphExplorerService(self.repository)

    def test_live_summary_topology_analytics_and_candidate_exclusion(self):
        summary = self.service.get_graph_summary()
        self.assertEqual(summary["documents"], 4)
        self.assertEqual(summary["departments"], 2)
        self.assertEqual(summary["validated_relations"], 6)
        self.assertEqual(summary["validated_document_relations"], 2)
        self.assertEqual(summary["relation_counts"]["REFERENCES"], 2)
        self.assertEqual(summary["relation_counts"]["ISSUED_BY"], 4)
        self.assertEqual(summary["relation_counts"]["AMENDS"], 0)
        self.assertEqual(summary["connected_documents"], 3)
        self.assertEqual(summary["isolated_documents"], 1)
        self.assertEqual(summary["components"], 2)
        self.assertEqual(summary["largest_component"], 3)
        self.assertEqual(summary["top_cited"][0]["count"], 1)
        self.assertEqual(summary["top_citing"][0]["count"], 1)
        self.assertEqual(len(summary["graph_fingerprint"]), 64)
        self.assertEqual(summary["total_documents"], 4)
        self.assertEqual(summary["total_department_registry"], 2)
        self.assertEqual(summary["active_departments"], 2)
        self.assertEqual(summary["total_graph_nodes"], 6)
        self.assertEqual(summary["total_validated_edges"], 6)

    def test_full_graph_has_document_department_and_directed_edges(self):
        graph = self.service.get_full_graph()
        self.assertEqual(graph["returned"], {"nodes": 6, "documents": 4, "departments": 2, "edges": 6})
        self.assertEqual({node["type"] for node in graph["nodes"]}, {"DOCUMENT", "DEPARTMENT"})
        reference = next(edge for edge in graph["edges"] if edge["id"] == "relation:101")
        issued = next(edge for edge in graph["edges"] if edge["id"] == "relation:103")
        self.assertEqual((reference["source"], reference["target"]), ("document:1", "document:2"))
        self.assertEqual((issued["source"], issued["target"]), ("document:1", "department:10"))
        self.assertFalse(graph["candidate_edges_included"])
        self.assertNotIn("relation:107", {edge["id"] for edge in graph["edges"]})
        self.assertEqual(graph["counts"]["displayed_nodes"], 6)
        self.assertEqual(graph["counts"]["displayed_edges"], 6)
        self.assertEqual(graph["node_role_counts"], {
            "ISOLATED_DOCUMENT": 1,
            "REFERENCED_DOCUMENT": 1,
            "CITING_DOCUMENT": 1,
            "REFERENCED_AND_CITING_DOCUMENT": 1,
            "DEPARTMENT": 2,
            "CANDIDATE_OR_UNRESOLVED": 0,
        })

    def test_document_roles_use_only_validated_references(self):
        graph = self.service.get_full_graph()
        roles = {node["id"]: node.get("graph_role") for node in graph["nodes"]}
        self.assertEqual(roles["document:1"], "CITING_DOCUMENT")
        self.assertEqual(roles["document:2"], "REFERENCED_AND_CITING_DOCUMENT")
        self.assertEqual(roles["document:3"], "REFERENCED_DOCUMENT")
        self.assertEqual(roles["document:4"], "ISOLATED_DOCUMENT")
        self.assertIsNone(roles["department:10"])
        document_two = next(node for node in graph["nodes"] if node["id"] == "document:2")
        self.assertEqual(document_two["reference_degree"], {"incoming": 1, "outgoing": 1, "degree": 2})

        self.repository.state["relations"].append(_relation(108, 4, "AMENDS", target=1))
        refreshed = self.service.get_full_graph()
        refreshed_roles = {node["id"]: node.get("graph_role") for node in refreshed["nodes"]}
        self.assertEqual(refreshed_roles["document:4"], "ISOLATED_DOCUMENT")
        self.assertEqual(refreshed_roles["document:1"], "CITING_DOCUMENT")

    def test_documents_only_scope_excludes_administrative_nodes_and_edges(self):
        graph = self.service.get_full_graph(GraphFilters(graph_scope="documents"))
        self.assertEqual(graph["returned"], {"nodes": 4, "documents": 4, "departments": 0, "edges": 2})
        self.assertEqual({node["type"] for node in graph["nodes"]}, {"DOCUMENT"})
        self.assertEqual({edge["relation_type"] for edge in graph["edges"]}, {"REFERENCES"})
        self.assertEqual(graph["counts"]["displayed_references"], 2)
        self.assertEqual(graph["counts"]["displayed_issued_by"], 0)
        self.assertEqual(graph["counts"]["total_graph_nodes"], 6)

    def test_display_labels_are_human_readable_but_identity_is_preserved(self):
        document = deepcopy(DOCUMENTS[2])
        document["gr_number"] = ""
        document["subject"] = "The Environment (Protection) Act, 1986"
        document["canonical_identity"] = "ENVIRONMENT_PROTECTION_ACT_1986"
        node = self.service._document_node(document, {"incoming": 0, "outgoing": 0, "degree": 0})
        self.assertEqual(node["label"], "Environment Protection Act, 1986")
        self.assertEqual(node["metadata"]["canonical_identity"], "ENVIRONMENT_PROTECTION_ACT_1986")

    def test_neighborhood_depth_and_direction(self):
        one = self.service.get_document_neighborhood(1, direction="outgoing", depth=1)
        two = self.service.get_document_neighborhood(1, direction="outgoing", depth=2)
        incoming = self.service.get_document_neighborhood(3, direction="incoming", depth=1)
        both = self.service.get_document_neighborhood(2, direction="both", depth=1)
        docs = lambda graph: {node["metadata"]["document_id"] for node in graph["nodes"] if node["type"] == "DOCUMENT"}
        self.assertEqual(docs(one), {1, 2})
        self.assertEqual(docs(two), {1, 2, 3})
        self.assertEqual(docs(incoming), {2, 3})
        self.assertEqual(docs(both), {1, 2, 3})

    def test_search_and_all_document_filters(self):
        self.assertEqual(self.service.search_graph_nodes("Policy document 3")["items"][0]["id"], "document:3")
        filters = GraphFilters(document_kinds=("ACT",), departments=("Law",), year_min=2022,
                               year_max=2022, source_origins=("EXTERNAL_OFFICIAL",),
                               validation_states=("HUMAN_CONFIRMED",), relation_types=("REFERENCES",))
        graph = self.service.get_full_graph(filters)
        self.assertEqual([node["id"] for node in graph["nodes"]], ["document:3"])
        self.assertEqual(graph["edges"], [])
        isolated = self.service.get_full_graph(GraphFilters(connectivity="isolated"))
        self.assertEqual({node["id"] for node in isolated["nodes"] if node["type"] == "DOCUMENT"}, {"document:4"})
        outgoing = self.service.get_full_graph(GraphFilters(relationship_presence="outgoing"))
        self.assertEqual({node["id"] for node in outgoing["nodes"] if node["type"] == "DOCUMENT"}, {"document:1", "document:2"})

    def test_component_selection_and_truncation_are_explicit(self):
        components = self.service.get_components()
        self.assertEqual(components["items"][0]["component_id"], "component:1")
        focused = self.service.get_full_graph(GraphFilters(component_id="component:1"))
        self.assertEqual(focused["returned"]["documents"], 3)
        truncated = self.service.get_full_graph(GraphFilters(max_nodes=2, max_edges=1))
        self.assertTrue(truncated["truncated"])
        self.assertIn("limit", truncated["truncation_reason"])

    def test_node_and_edge_details_keep_provenance(self):
        document = self.service.get_node_details("document:1")
        department = self.service.get_node_details("department:10")
        edge = self.service.get_edge_details(101)
        self.assertEqual(document["type"], "DOCUMENT")
        self.assertEqual(document["graph_role"], "CITING_DOCUMENT")
        self.assertEqual(document["reference_degree"], {"incoming": 0, "outgoing": 1, "degree": 1})
        self.assertEqual(len(document["outgoing_relations"]), 2)
        self.assertEqual(document["connected_departments"], ["Finance"])
        self.assertEqual(department["department"]["aliases"], ["FD"])
        self.assertEqual(edge["direction"], "source_to_target")
        self.assertEqual(edge["evidence"][0]["source_page"], 2)
        self.assertEqual(edge["evidence"][0]["raw_evidence_text"], "Stored source-page evidence")
        self.assertEqual(edge["evidence"][0]["relation_evidence_id"], 501)
        self.assertTrue(edge["provenance_complete"])
        self.assertIsNone(self.service.get_edge_details(107))

    def test_dynamic_refresh_reads_a_new_snapshot_and_reverts_cleanly(self):
        before = self.service.get_graph_summary()
        self.repository.state["documents"].append(_document(5, kind="RULE", year=2024, department="Law"))
        self.repository.state["relations"].append(_relation(108, 5, "REFERENCES", target=4))
        refreshed = self.service.get_graph_summary()
        self.assertEqual((before["documents"], refreshed["documents"]), (4, 5))
        self.assertNotEqual(before["graph_fingerprint"], refreshed["graph_fingerprint"])
        self.repository.state["documents"].pop()
        self.repository.state["relations"].pop()
        rolled_back = self.service.get_graph_summary()
        self.assertEqual(before["graph_fingerprint"], rolled_back["graph_fingerprint"])

    def test_filter_validation_rejects_untrusted_or_invalid_values(self):
        with self.assertRaises(ExplorerValidationError):
            GraphFilters(validation_states=("CANDIDATE",)).normalized()
        with self.assertRaises(ExplorerValidationError):
            GraphFilters(relation_types=("INVENTED",)).normalized()
        with self.assertRaises(ExplorerValidationError):
            GraphFilters(graph_scope="invented").normalized()
        with self.assertRaises(ExplorerValidationError):
            self.service.get_document_neighborhood(1, direction="sideways", depth=1)


class GraphExplorerApiTests(AuthorizedViewTestCase):
    def setUp(self):
        super().setUp()
        self.service = GraphExplorerService(FakeExplorerRepository())
        self.patcher = patch("apps.knowledge_graph_ui.views.get_explorer_service", return_value=self.service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_page_and_get_only_api_contract(self):
        page = self.client.get(reverse("knowledge_graph_ui:overview"))
        self.assertContains(page, "Knowledge graph explorer")
        self.assertContains(page, "Fullscreen")
        self.assertContains(page, "Documents")
        self.assertContains(page, "Administrative")
        self.assertContains(page, "cytoscape-3.34.2.min.js")
        for name, args in (
            ("api_summary", ()), ("api_graph", ()), ("api_search", ()),
            ("api_components", ()), ("api_neighborhood", (1,)),
            ("api_node_detail", ("document:1",)), ("api_edge_detail", (101,)),
        ):
            url = reverse(f"knowledge_graph_ui:{name}", args=args)
            self.assertEqual(self.client.get(url, {"q": "policy"}).status_code, 200)
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.post(reverse("knowledge_graph_ui:overview")).status_code, 405)

    def test_api_validation_and_json_export(self):
        invalid = self.client.get(reverse("knowledge_graph_ui:api_graph"), {"validation": "CANDIDATE"})
        self.assertEqual(invalid.status_code, 400)
        response = self.client.get(reverse("knowledge_graph_ui:api_graph"), {"download": "1"})
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertTrue(response.json()["read_only"])
        documents = self.client.get(reverse("knowledge_graph_ui:api_graph"), {"scope": "documents"}).json()
        self.assertEqual(documents["filters"]["graph_scope"], "documents")
        self.assertEqual(documents["returned"]["departments"], 0)

    def test_no_visualization_write_route_exists(self):
        from apps.knowledge_graph_ui.urls import urlpatterns

        names = {pattern.name for pattern in urlpatterns}
        self.assertFalse(any(value and any(word in value for word in ("create", "update", "delete", "approve"))
                             for value in names))

    def test_offline_assets_are_local(self):
        project = Path(__file__).resolve().parents[1]
        template = (project / "templates/knowledge_graph/overview.html").read_text(encoding="utf-8")
        script = (project / "static/knowledge_graph/explorer.js").read_text(encoding="utf-8")
        stylesheet = (project / "static/knowledge_graph/explorer.css").read_text(encoding="utf-8")
        self.assertNotIn("https://", template + script + stylesheet)
        self.assertNotIn("http://", template + script + stylesheet)
        self.assertTrue((project / "static/vendor/cytoscape/cytoscape-3.34.2.min.js").is_file())
        self.assertTrue((project / "static/vendor/cytoscape/LICENSE").is_file())

    def test_production_ui_controls_and_states_are_present(self):
        project = Path(__file__).resolve().parents[1]
        template = (project / "templates/knowledge_graph/overview.html").read_text(encoding="utf-8")
        script = (project / "static/knowledge_graph/explorer.js").read_text(encoding="utf-8")
        stylesheet = (project / "static/knowledge_graph/explorer.css").read_text(encoding="utf-8")
        for marker in (
            "data-focus-mode", "data-fullscreen", "data-filter-toggle", "data-inspector-toggle",
            "data-export-png", "data-edge-labels", "data-layout", "data-connectivity",
            "data-neighborhood", "data-legend", 'data-state="LOADING"',
        ):
            self.assertIn(marker, template)
        for behavior in (
            "requestFullscreen", "document.exitFullscreen", "state.graphScope", "state.activeFilters",
            "state.selectedId", "state.refreshSeconds", "Selection removed", "No graph elements match",
            "Graph API unavailable", "Database or graph service unavailable", ".png(",
            "validateDomContract", "CLIENT_RENDER_ERROR", "FILTERED_TO_ZERO", "EMPTY_GRAPH",
            "window.__kgExplorerReady", "OPTIONAL_SUMMARY_ERROR",
        ):
            self.assertIn(behavior, script)
        self.assertNotIn("radial-gradient", stylesheet)
        self.assertIn("background-image:none!important", stylesheet)
        self.assertIn("explorer.css' %}?v=2.2.0", template)
        self.assertIn("explorer.js' %}?v=2.2.0", template)
        self.assertIn("Referenced + citing", template)
        self.assertIn("role-referenced-and-citing-document", script)
        self.assertIn("validated REFERENCES only", script)
        self.assertIn("<span data-visible-nodes>—</span>", template)
        self.assertIn("<span data-visible-edges>—</span>", template)

    def test_v21_critical_dom_contract_matches_javascript(self):
        project = Path(__file__).resolve().parents[1]
        template = (project / "templates/knowledge_graph/overview.html").read_text(encoding="utf-8")
        base = (project / "templates/base.html").read_text(encoding="utf-8")
        script = (project / "static/knowledge_graph/explorer.js").read_text(encoding="utf-8")
        critical = (
            'id="knowledge-graph-canvas"', "data-visible-nodes", "data-visible-edges",
            "data-graph-message", "data-graph-shell", "data-refresh", "data-summary-panel",
            "data-filter-panel", "data-inspector", "data-focus-mode",
            "data-fullscreen", "data-legend",
        )
        for marker in critical:
            self.assertIn(marker, template)
        self.assertIn("data-sidebar-toggle", base)
        self.assertNotIn("data-sidebar-toggle", template)
        for selector in (
            "#knowledge-graph-canvas", "[data-visible-nodes]", "[data-visible-edges]",
            "[data-graph-message]", "[data-graph-shell]",
        ):
            self.assertIn(selector, script)

    def test_v21_browser_harness_is_development_only(self):
        project = Path(__file__).resolve().parents[1]
        development_requirements = (project / "requirements-dev.txt").read_text(encoding="utf-8")
        production_requirements = (project / "requirements.txt").read_text(encoding="utf-8")
        harness = (project / "tests/browser/test_knowledge_graph_explorer.py").read_text(encoding="utf-8")
        self.assertIn("playwright==1.62.0", development_requirements)
        self.assertNotIn("playwright", production_requirements.lower())
        self.assertIn("RUN_BROWSER_E2E", harness)


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 for native PostgreSQL graph explorer tests",
)
def test_native_postgres_explorer_is_read_only_and_preserves_live_state():
    from knowledge_engine.retrieval.graph_eval import live_state

    before = live_state()
    repository = GraphExplorerRepository()
    with repository._read_connection() as connection:
        read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
    graph = GraphExplorerService(repository).get_full_graph()
    after = live_state()
    assert read_only == "on"
    assert graph["summary"]["runtime"]["server_port"] == 5434
    for key in ("documents", "pages", "chunks", "embeddings", "kg_candidates", "kg_relations",
                "retrieval_corpus_fingerprint", "kg_relation_fingerprint"):
        assert after[key] == before[key]
