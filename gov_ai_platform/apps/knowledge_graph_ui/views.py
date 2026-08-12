from django.http import Http404
from django.shortcuts import render

from knowledge_engine.graph.service import GraphService
from knowledge_engine.portal import PortalRepository


def get_graph_service():
    return GraphService()


def get_portal_repository():
    return PortalRepository()


def overview(request):
    return render(request, "knowledge_graph/overview.html", {
        "graph": get_graph_service().get_graph_summary(),
        "active_section": "graph", "context_mode": "graph",
    })


def unresolved_list(request):
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    return render(request, "knowledge_graph/unresolved_list.html", {
        "listing": get_portal_repository().list_unresolved_groups(page=page),
        "active_section": "unresolved", "context_mode": "unresolved",
    })


def unresolved_detail(request, candidate_id: int):
    group = get_portal_repository().get_unresolved_group(candidate_id)
    if group is None:
        raise Http404("Unresolved target not found")
    return render(request, "knowledge_graph/unresolved_detail.html", {
        "group": group, "active_section": "unresolved", "context_mode": "unresolved",
    })
