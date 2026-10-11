from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.urls import reverse

from apps.identity_access.models import AccessGrant, AccessRole, IASAuthorityScope, OrganizationUnit
from tests.access_test_helpers import AuthorizedViewTestCase


DOCUMENT = {
    "id": 7, "file_name": "source.pdf", "department": "Finance Department",
    "document_type": "Government Resolution", "gr_number": "FIN/2024/7",
    "gr_date": date(2024, 1, 2), "subject": "Administrative policy",
    "page_count": 3, "digital_pages": 2, "ocr_pages": 1,
    "extraction_method": "mixed", "metadata": {}, "created_at": None, "chunk_count": 2,
}


class FakePortalRepository:
    def departments(self): return ["Finance Department"]
    def years(self): return [2024]
    def list_documents(self, **kwargs):
        self.document_filters = kwargs
        return {"items": [DOCUMENT], "total": 1, "page": 1, "pages": 1}
    def get_document_workspace(self, document_id): return DOCUMENT if document_id == 7 else None
    def list_chunks(self, document_id, **kwargs):
        return {"items": [{"id": 1, "chunk_index": 0, "page_start": 1, "page_end": 2,
                            "token_count": 120, "text": "Stored evidence"}], "total": 1, "page": 1, "pages": 1}
    def list_unresolved_groups(self, **kwargs):
        return {"items": [{"candidate_id": 11, "normalized_document_number": "abc-1",
                            "reference_kind": "GOVERNMENT_RESOLUTION", "normalized_reference_date": None,
                            "mentions": 2, "source_documents": 1, "source_department": "Finance Department"}],
                "total": 1, "page": 1, "pages": 1}
    def get_unresolved_group(self, candidate_id):
        if candidate_id != 11: return None
        return {"target": {"normalized_document_number": "abc-1", "reference_kind": "GOVERNMENT_RESOLUTION",
                           "normalized_reference_date": None}, "mentions": 1, "source_documents": 1,
                "items": [{"candidate_id": 11, "source_page": 2, "raw_evidence_text": "cited evidence",
                           "resolution_status": "NOT_IN_CORPUS", "validation_status": "UNRESOLVED",
                           "source_document_id": 7, "source_file": "source.pdf", "source_department": "Finance"}]}


class FakeGraphService:
    def get_graph_summary(self):
        return {"documents_represented": 61, "departments_represented": 8, "reference_candidates": 729,
                "resolved_candidates": 1, "auto_verified_candidates": 1, "unresolved_candidates": 368,
                "ambiguous_candidates": 1, "unparseable_candidates": 122, "references_edges": 1,
                "issued_by_edges": 61}
    def get_document_detail(self, document_id):
        return {"document": DOCUMENT, "issued_by": {"canonical_name": "Finance Department", "validation_status": "AUTO_VERIFIED"},
                "outgoing_references": [], "incoming_references": [], "unresolved_references": [], "relation_signals": []}


class FakeExplorerService:
    def get_graph_summary(self):
        return {
            "documents": 87, "departments": 12, "validated_relations": 137,
            "validated_document_relations": 50, "relation_counts": {
                "REFERENCES": 50, "ISSUED_BY": 87, "AMENDS": 0, "CORRECTS": 0,
                "SUPERSEDES": 0, "PARTIALLY_SUPERSEDES": 0, "CANCELS": 0, "EXTENDS": 0,
            },
            "connected_documents": 35, "isolated_documents": 52, "graph_coverage": 35 / 87,
            "components": 54, "largest_component": 14, "top_cited": [], "top_citing": [],
            "most_connected": [], "department_document_counts": [],
            "filter_options": {"relation_types": [], "document_kinds": [], "departments": [],
                               "years": [], "validation_states": [], "source_origins": []},
            "runtime": {"postgresql_version": "17.11"}, "graph_fingerprint": "a" * 64,
            "generated_at": "2026-09-02T00:00:00+00:00", "read_only": True,
        }


class FakePortalService:
    def corpus_dashboard(self):
        return {"summary": {"documents": 61, "pages": 1022, "chunks": 740, "embeddings": 740,
                            "departments": 8, "embedding_dimension": 1024, "chunks_over_limit": 0,
                            "invalid_embeddings": 0, "duplicate_paths": 0, "duplicate_sha": 0,
                            "digital_pages": 900, "ocr_pages": 122, "status": "PASS"},
                "extraction": [{"extraction_method": "digital", "count": 900}],
                "graph": FakeGraphService().get_graph_summary(), "artifact_integrity": {"status": "PASS"},
                "generated_at": "2026-08-13"}
    def evaluation_dashboard(self):
        metric = {"recall_at_1": .8, "recall_at_3": 1, "recall_at_5": 1, "recall_at_10": 1, "mrr": .9}
        return {"retrieval": {"after": {"question_count": 29, "model": "BAAI/bge-m3", "document": metric, "page": metric},
                              "unchanged": True, "dense_retrieval_formula_changed": False},
                "parser": {"label_source": "CODEX_SOURCE_REVIEW", "reference_detection_precision": .98,
                           "reference_detection_recall": .95, "document_number_extraction_accuracy": .98,
                           "date_extraction_accuracy": 1.0}, "generated_at": "2026-08-13"}


class PortalShellAndSearchTests(AuthorizedViewTestCase):
    @patch("apps.search_ui.views.get_portal_repository", return_value=FakePortalRepository())
    @patch("apps.search_ui.views.get_retrieval_service")
    def test_get_is_chat_first_and_does_not_construct_retrieval_service(self, retrieval, _repository):
        response = self.client.get(reverse("search_ui:search"))
        self.assertEqual(response.status_code, 200)
        retrieval.assert_not_called()
        for text in ("Government Administrative AI Copilot", "Documents", "Knowledge graph", "Corpus health",
                     "does not generate an AI answer", "Ctrl / ⌘ + Enter"):
            self.assertContains(response, text)
        self.assertContains(response, "data-sidebar-toggle")
        self.assertContains(response, "Collapse application navigation")
        self.assertContains(response, "portal/sidebar.css")

    @patch("apps.search_ui.views.get_portal_repository", return_value=FakePortalRepository())
    @patch("apps.search_ui.views.get_retrieval_service")
    def test_shared_shell_separates_header_navigation_and_account(self, retrieval, _repository):
        response = self.client.get(reverse("search_ui:search"))
        self.assertEqual(response.status_code, 200)
        retrieval.assert_not_called()
        shell = response.content.decode()
        header = shell.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
        sidebar = shell.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
        account_panel = header.split('id="account-menu-panel"', 1)[1]

        self.assertIn('data-shell-layout', shell)
        self.assertIn('data-sidebar-scroll', sidebar)
        self.assertNotIn('account-menu', sidebar)
        self.assertNotIn('authorized-view-tester', sidebar)
        self.assertIn('data-account-menu-toggle', header)
        self.assertIn('authorized-view-tester', header)
        self.assertIn('Government Officer', header)
        self.assertIn('method="post"', account_panel)
        self.assertIn(reverse("identity_access:logout"), account_panel)
        self.assertIn('role="menuitem"', account_panel)
        self.assertIn('aria-current="page"', sidebar)
        self.assertNotIn('Access requests', sidebar)
        self.assertNotIn('Approval cases', sidebar)

    @patch("apps.search_ui.views.get_portal_repository", return_value=FakePortalRepository())
    @patch("apps.search_ui.views.get_retrieval_service")
    def test_ias_navigation_remains_scope_authorized(self, retrieval, _repository):
        user = get_user_model().objects.get(username="authorized-view-tester")
        organization = OrganizationUnit.objects.get(code="test-unit")
        AccessGrant.objects.create(user=user, organization=organization, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=user, organization=organization)

        response = self.client.get(reverse("search_ui:search"))
        self.assertEqual(response.status_code, 200)
        retrieval.assert_not_called()
        shell = response.content.decode()
        sidebar = shell.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
        for label in ("Access requests", "Officer access", "Audit log", "Approval cases"):
            self.assertIn(label, sidebar)
        self.assertContains(response, "IAS Officer")

    def test_shell_css_and_behavior_contract(self):
        css = (Path(settings.BASE_DIR) / "static" / "portal" / "sidebar.css").read_text(encoding="utf-8")
        javascript = (Path(settings.BASE_DIR) / "static" / "portal" / "app.js").read_text(encoding="utf-8")
        for token in (
            "--app-header-height: 64px",
            "--sidebar-expanded-width: 240px",
            "--sidebar-collapsed-width: 76px",
            "grid-template-rows: var(--app-header-height) minmax(0, 1fr)",
            ".sidebar-navigation",
            "overflow-y: auto",
            ".workspace > .main-content",
            "@media (max-width: 900px)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(token, css)
        self.assertNotIn(".sidebar-account", css)
        for token in (
            "data-account-menu-toggle",
            "event.key !== 'Escape'",
            "event.key === 'ArrowDown'",
            "data-nav-dismiss",
            "app-sidebar-collapsed",
        ):
            self.assertIn(token, javascript)

    @patch("apps.search_ui.views.get_portal_repository", return_value=FakePortalRepository())
    @patch("apps.search_ui.views.get_retrieval_service")
    def test_search_preserves_controls_and_passes_department(self, retrieval_factory, _repository):
        retrieval_factory.return_value.search.return_value = [SimpleNamespace(
            document_id=7, rank=1, similarity=.91, file_name="source.pdf", department="Finance Department",
            gr_number="FIN/2024/7", gr_date=date(2024, 1, 2), subject="Policy", page_start=1, page_end=1,
            text="Grounded passage")]
        response = self.client.post(reverse("search_ui:search"), {
            "query": "policy question", "top_k": "3", "department": "Finance Department"})
        self.assertContains(response, "Grounded passage")
        self.assertContains(response, "Selected source")
        retrieval_factory.return_value.search.assert_called_once_with(
            "policy question", top_k=3, department="Finance Department")


class DocumentWorkspaceTests(AuthorizedViewTestCase):
    @patch("apps.documents.views.get_portal_repository", return_value=FakePortalRepository())
    def test_library_filters_and_bounded_listing(self, _repository):
        response = self.client.get(reverse("documents:list"), {"q": "policy", "department": "Finance Department",
                                                                 "year": "2024", "sort": "filename"})
        self.assertContains(response, "Document library")
        self.assertContains(response, "Administrative policy")

    @patch("apps.documents.views.get_portal_repository", return_value=FakePortalRepository())
    def test_evidence_tab_has_pdf_page_link(self, _repository):
        response = self.client.get(reverse("documents:detail", args=[7]), {"tab": "evidence"})
        self.assertContains(response, "Stored evidence")
        self.assertContains(response, "Show in PDF at page 1")

    @patch("apps.documents.views.get_portal_repository", return_value=FakePortalRepository())
    def test_pdf_tab_uses_safe_pdf_endpoint(self, _repository):
        response = self.client.get(reverse("documents:detail", args=[7]), {"tab": "pdf"})
        self.assertContains(response, reverse("documents:pdf", args=[7]))

    @patch("apps.documents.views.get_graph_service", return_value=FakeGraphService())
    @patch("apps.documents.views.get_portal_repository", return_value=FakePortalRepository())
    def test_relationship_tab_labels_verified_and_candidate_states(self, _repository, _graph):
        response = self.client.get(reverse("documents:detail", args=[7]), {"tab": "relationships"})
        self.assertContains(response, "Issued By")
        self.assertContains(response, "Unresolved References")
        self.assertContains(response, "Candidate relationship")


class GraphAndSystemWorkspaceTests(AuthorizedViewTestCase):
    @patch("apps.knowledge_graph_ui.views.get_explorer_service", return_value=FakeExplorerService())
    def test_graph_overview(self, _service):
        response = self.client.get(reverse("knowledge_graph_ui:overview"))
        self.assertContains(response, "Knowledge graph explorer")
        self.assertContains(response, "Candidate edges are excluded")

    @patch("apps.knowledge_graph_ui.views.document_summaries", return_value=[])
    @patch("apps.knowledge_graph_ui.views.evidence_for_work_item")
    def test_unresolved_list_and_detail(self, evidence_mock, _documents):
        from apps.identity_access.models import OrganizationUnit
        from apps.knowledge_graph_ui.models import (
            UnresolvedReferenceCandidateLink,
            UnresolvedReferenceWorkItem,
            WorkItemType,
        )

        organization = OrganizationUnit.objects.get(code="test-unit")
        item = UnresolvedReferenceWorkItem.objects.create(
            target_fingerprint="d" * 64,
            organization=organization,
            display_identifier="abc-1",
            normalized_target_identity="abc-1",
            reference_kind="GOVERNMENT_RESOLUTION",
            work_type=WorkItemType.MISSING_DOCUMENT,
            mention_count=1,
            distinct_source_document_count=1,
        )
        UnresolvedReferenceCandidateLink.objects.create(
            work_item=item,
            candidate_id=11,
            source_document_id=7,
            source_page=1,
            candidate_fingerprint="e" * 64,
        )
        evidence_mock.return_value = {
            "items": [{
                "source_department": "Finance Department",
                "resolution_status": "NOT_IN_CORPUS",
                "source_document_id": 7,
                "source_file": "source.pdf",
                "raw_evidence_text": "cited evidence",
                "source_page": 1,
                "validation_status": "UNRESOLVED",
            }],
            "page": 1,
            "pages": 1,
            "total": 1,
        }
        self.assertContains(self.client.get(reverse("knowledge_graph_ui:unresolved")), "abc-1")
        detail = self.client.get(reverse("knowledge_graph_ui:unresolved_detail", args=[11]), follow=True)
        self.assertContains(detail, "cited evidence")
        self.assertContains(detail, "Original evidence remains")

    @patch("apps.system_ui.views.get_portal_service", return_value=FakePortalService())
    def test_corpus_dashboard_uses_observability_language(self, _service):
        response = self.client.get(reverse("system_ui:corpus"))
        self.assertContains(response, "1022")
        self.assertContains(response, "never mutates the corpus")

    @patch("apps.system_ui.views.get_portal_service", return_value=FakePortalService())
    def test_evaluation_disclaimer_and_truth_labels(self, _service):
        response = self.client.get(reverse("system_ui:evaluation"))
        self.assertContains(response, "Controlled 61-document evaluation")
        self.assertContains(response, "not independent human ground truth")
