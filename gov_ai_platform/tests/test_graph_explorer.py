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
            self.service.get_document_neighborhood(1, direction="sideways", depth=1)


class GraphExplorerApiTests(SimpleTestCase):
    def setUp(self):
        self.service = GraphExplorerService(FakeExplorerRepository())
        self.patcher = patch("apps.knowledge_graph_ui.views.get_explorer_service", return_value=self.service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_page_and_get_only_api_contract(self):
        page = self.client.get(reverse("knowledge_graph_ui:overview"))
        self.assertContains(page, "Knowledge graph explorer")
        self.assertContains(page, "Refresh graph")
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
