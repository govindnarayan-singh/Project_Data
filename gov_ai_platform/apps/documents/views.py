from __future__ import annotations

from django.http import FileResponse, Http404
from django.shortcuts import render

from knowledge_engine.graph.service import GraphService
from knowledge_engine.portal import PortalRepository
from knowledge_engine.retrieval.repository import PostgresRetrievalRepository
from knowledge_engine.settings import settings

from .services import UnsafeDocumentPath, resolve_pdf_path


def get_document_repository() -> PostgresRetrievalRepository:
    return PostgresRetrievalRepository()


def get_graph_service() -> GraphService:
    return GraphService()


def get_portal_repository() -> PortalRepository:
    return PortalRepository()


def document_list(request):
    query = request.GET.get("q", "").strip()[:200]
    department = request.GET.get("department", "").strip()[:200]
    sort = request.GET.get("sort", "date_desc")
    try:
        year = int(request.GET["year"]) if request.GET.get("year") else None
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        year, page = None, 1
    repository = get_portal_repository()
    listing = repository.list_documents(query=query, department=department, year=year, sort=sort, page=page)
    return render(request, "documents/list.html", {
        "listing": listing, "q": query, "department": department, "year": year, "sort": sort,
        "departments": repository.departments(), "years": repository.years(),
        "active_section": "documents", "context_mode": "filters",
    })


def document_detail(request, document_id: int):
    repository = get_portal_repository()
    document = repository.get_document_workspace(document_id)
    if document is None:
        raise Http404("Document not found")
    tab = request.GET.get("tab", "relationships")
    if tab not in {"overview", "evidence", "pdf", "relationships", "metadata"}:
        tab = "overview"
    context = {"document": document, "tab": tab, "active_section": "documents", "context_mode": "document"}
    if tab == "evidence":
        try:
            chunk_page = max(1, int(request.GET.get("page", "1")))
        except ValueError:
            chunk_page = 1
        context["chunks"] = repository.list_chunks(document_id, page=chunk_page)
    if tab == "relationships":
        graph = get_graph_service().get_document_detail(document_id) or {}
        graph.pop("document", None)
        context.update(graph)
    return render(request, "documents/detail.html", context)


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
