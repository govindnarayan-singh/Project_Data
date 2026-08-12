from __future__ import annotations

from django.http import FileResponse, Http404
from django.shortcuts import render

from knowledge_engine.graph.service import GraphService
from knowledge_engine.retrieval.repository import PostgresRetrievalRepository
from knowledge_engine.settings import settings

from .services import UnsafeDocumentPath, resolve_pdf_path


def get_document_repository() -> PostgresRetrievalRepository:
    return PostgresRetrievalRepository()


def get_graph_service() -> GraphService:
    return GraphService()


def document_detail(request, document_id: int):
    detail = get_graph_service().get_document_detail(document_id)
    if detail is None:
        raise Http404("Document not found")
    return render(request, "documents/detail.html", detail)


def document_pdf(request, document_id: int):
    document = get_document_repository().get_document(document_id)
    if document is None:
        raise Http404("Document not found")
    try:
        pdf_path = resolve_pdf_path(settings.gr_root, document["file_path"])
        handle = pdf_path.open("rb")
    except (UnsafeDocumentPath, FileNotFoundError, OSError) as error:
        raise Http404("PDF not available") from error
    response = FileResponse(handle, content_type="application/pdf")
    safe_name = str(document["file_name"]).replace('"', "")
    response["Content-Disposition"] = f'inline; filename="{safe_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
