from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from apps.documents.services import UnsafeDocumentPath, resolve_pdf_path
from tests.access_test_helpers import AuthorizedViewTestCase


class FakeSearchService:
    def search(self, query, top_k=5, department=None):
        return [SimpleNamespace(
            document_id=9, rank=1, similarity=0.9123, file_name="मराठी.pdf",
            department="Finance", gr_number="शानि-१", gr_date=None,
            subject="मराठी विषय", page_start=2, page_end=3, text="मराठी पुरावा",
        )]


class SearchViewTests(AuthorizedViewTestCase):
    def test_get_and_marathi_post_render(self):
        self.assertContains(self.client.get(reverse("search_ui:search")), "Government Administrative Knowledge Search")
        with patch("apps.search_ui.views.get_retrieval_service", return_value=FakeSearchService()):
            response = self.client.post(reverse("search_ui:search"), {"query": "मराठी प्रश्न", "top_k": "5"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "मराठी प्रश्न")
        self.assertContains(response, "मराठी पुरावा")
        self.assertContains(response, "does not generate an AI answer")

    def test_invalid_query_and_top_k(self):
        self.assertEqual(self.client.post(reverse("search_ui:search"), {"query": "", "top_k": "5"}).status_code, 400)
        self.assertEqual(self.client.post(reverse("search_ui:search"), {"query": "x", "top_k": "99"}).status_code, 400)


class FakeDocumentRepository:
    def __init__(self, document): self.document = document
    def get_document(self, document_id): return self.document


class PdfEndpointTests(AuthorizedViewTestCase):
    def test_safe_pdf_path_and_inline_response(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pdf = root / "Department" / "evidence.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-1.4\n%%EOF")
            repository = FakeDocumentRepository({"id": 2, "file_name": "evidence.pdf", "file_path": "Department/evidence.pdf"})
            with patch("apps.documents.views.get_document_repository", return_value=repository), patch("apps.documents.views.settings", SimpleNamespace(gr_root=root)):
                response = self.client.get(reverse("documents:pdf", args=[2]))
                body = b"".join(response.streaming_content)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertIn("inline", response["Content-Disposition"])
            self.assertTrue(body.startswith(b"%PDF"))

    def test_unknown_document_id(self):
        with patch("apps.documents.views.get_document_repository", return_value=FakeDocumentRepository(None)):
            self.assertEqual(self.client.get(reverse("documents:pdf", args=[999])).status_code, 404)

    def test_traversal_and_outside_absolute_path_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for value in ("../secret.pdf", str(root.parent / "secret.pdf")):
                with self.subTest(value=value), self.assertRaises(UnsafeDocumentPath):
                    resolve_pdf_path(root, value)


class FakeGraphService:
    def get_document_detail(self, document_id):
        if document_id != 4:
            return None
        evidence = "<script>alert('unsafe')</script>"
        return {
            "document": {
                "id": 4, "file_name": "source.pdf", "file_path": "source.pdf",
                "department": "Finance", "document_type": "Government Resolution",
                "gr_number": "ABC/2020/1", "gr_date": None, "subject": "Subject",
                "page_count": 2, "chunk_count": 1,
            },
            "issued_by": {"canonical_name": "Finance", "validation_status": "AUTO_VERIFIED"},
            "outgoing_references": [{
                "relation_id": 1, "validation_status": "AUTO_VERIFIED", "target_document_id": 5,
                "target_file": "target.pdf", "source_page": 1, "raw_evidence_text": evidence,
                "raw_document_number": "XYZ/2020/2", "normalized_document_number": "xyz/2020/2",
                "resolution_status": "RESOLVED_EXACT", "reference_kind": "GOVERNMENT_RESOLUTION",
            }],
            "incoming_references": [{
                "relation_id": 2, "validation_status": "AUTO_VERIFIED", "source_document_id": 6,
                "source_file": "incoming.pdf", "source_page": 1, "raw_evidence_text": "incoming evidence",
                "raw_document_number": "ABC/2020/1", "normalized_document_number": "abc/2020/1",
                "resolution_status": "RESOLVED_EXACT", "reference_kind": "GOVERNMENT_RESOLUTION",
            }],
            "unresolved_references": [{
                "candidate_id": 3, "source_page": 2, "reference_kind": "ACT",
                "raw_evidence_text": "Missing Act, 2016", "raw_document_number": "Missing Act, 2016",
                "normalized_document_number": "MISSING_ACT_2016", "resolution_status": "NOT_IN_CORPUS",
                "validation_status": "UNRESOLVED",
            }],
            "relation_signals": [{
                "candidate_id": 4, "source_page": 2, "candidate_relation_type": "AMENDS",
                "relation_signal": "amended", "raw_evidence_text": "potentially amended",
                "validation_status": "CANDIDATE",
            }],
        }


class DocumentDetailTests(AuthorizedViewTestCase):
    def test_detail_displays_graph_categories_provenance_pdf_and_escapes_evidence(self):
        with patch("apps.documents.views.get_graph_service", return_value=FakeGraphService()):
            response = self.client.get(reverse("documents:detail", args=[4]))
        self.assertEqual(response.status_code, 200)
        for text in ("Outgoing References", "Incoming References", "Unresolved References", "Issued By", "Open source PDF", "Referenced document not currently available in corpus", "Candidate relationship"):
            self.assertContains(response, text)
        self.assertContains(response, "&lt;script&gt;", html=False)
        self.assertNotContains(response, "<script>alert")

    def test_unknown_document_detail_id(self):
        with patch("apps.documents.views.get_graph_service", return_value=FakeGraphService()):
            self.assertEqual(self.client.get(reverse("documents:detail", args=[999])).status_code, 404)
