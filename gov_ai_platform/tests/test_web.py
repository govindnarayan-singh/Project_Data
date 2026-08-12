from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from apps.documents.services import UnsafeDocumentPath, resolve_pdf_path


class FakeSearchService:
    def search(self, query, top_k=5, department=None):
        return [SimpleNamespace(
            document_id=9, rank=1, similarity=0.9123, file_name="मराठी.pdf",
            department="Finance", gr_number="शानि-१", gr_date=None,
            subject="मराठी विषय", page_start=2, page_end=3, text="मराठी पुरावा",
        )]


class SearchViewTests(SimpleTestCase):
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


class PdfEndpointTests(SimpleTestCase):
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
