from __future__ import annotations

from django.shortcuts import render

from knowledge_engine.portal import PortalRepository
from knowledge_engine.retrieval.service import ALLOWED_TOP_K, RetrievalService


def get_retrieval_service() -> RetrievalService:
    return RetrievalService()


def get_portal_repository() -> PortalRepository:
    return PortalRepository()


def search_view(request):
    try:
        departments = get_portal_repository().departments()
    except Exception:
        departments = []
    context = {"query": "", "department": "", "departments": departments,
               "top_k": 5, "top_k_options": sorted(ALLOWED_TOP_K), "results": [],
               "active_section": "copilot", "context_mode": "source"}
    if request.method == "POST":
        query = request.POST.get("query", "").strip()
        context["query"] = query
        department = request.POST.get("department", "").strip()
        if department and department not in departments:
            context["error"] = "Choose a valid department."
            return render(request, "search_ui/search.html", context, status=400)
        context["department"] = department
        try:
            top_k = int(request.POST.get("top_k", "5"))
            if top_k not in ALLOWED_TOP_K:
                raise ValueError
            context["top_k"] = top_k
        except (TypeError, ValueError):
            context["error"] = "Choose a valid result count: 3, 5, or 10."
            return render(request, "search_ui/search.html", context, status=400)
        if not query:
            context["error"] = "Enter a search question."
            return render(request, "search_ui/search.html", context, status=400)
        try:
            context["results"] = get_retrieval_service().search(query, top_k=top_k, department=department or None)
        except Exception:
            context["error"] = "Search is temporarily unavailable. Please try again."
            return render(request, "search_ui/search.html", context, status=503)
    return render(request, "search_ui/search.html", context)
