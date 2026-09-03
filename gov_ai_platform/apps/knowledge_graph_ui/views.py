from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from knowledge_engine.graph.explorer import (
    ExplorerValidationError,
    GraphExplorerService,
    GraphFilters,
)
from knowledge_engine.portal import PortalRepository


def get_portal_repository():
    return PortalRepository()


def get_explorer_service():
    return GraphExplorerService()


@require_GET
def overview(request):
    summary = get_explorer_service().get_graph_summary()
    summary["graph_coverage_percent"] = summary["graph_coverage"] * 100
    return render(request, "knowledge_graph/overview.html", {
        "graph": summary,
        "api": {
            "summary": reverse("knowledge_graph_ui:api_summary"),
            "graph": reverse("knowledge_graph_ui:api_graph"),
            "search": reverse("knowledge_graph_ui:api_search"),
            "components": reverse("knowledge_graph_ui:api_components"),
            "neighborhood_template": reverse("knowledge_graph_ui:api_neighborhood", args=[0]),
            "node_template": reverse("knowledge_graph_ui:api_node_detail", args=["node-placeholder"]),
            "edge_template": reverse("knowledge_graph_ui:api_edge_detail", args=[0]),
        },
        "active_section": "graph", "context_mode": "graph",
    })


def _values(request, name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in request.GET.getlist(name):
        values.extend(item.strip() for item in raw.split(",") if item.strip())
    return tuple(values)


def _integer(request, name: str, default: int | None = None) -> int | None:
    raw = request.GET.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ExplorerValidationError(f"{name} must be an integer") from error


def _filters(request) -> GraphFilters:
    return GraphFilters(
        relation_types=_values(request, "relation"),
        document_kinds=_values(request, "kind"),
        departments=_values(request, "department"),
        validation_states=_values(request, "validation"),
        source_origins=_values(request, "source_origin"),
        year_min=_integer(request, "year_min"),
        year_max=_integer(request, "year_max"),
        connectivity=request.GET.get("connectivity", "all"),
        relationship_presence=request.GET.get("presence", "any"),
        component_id=request.GET.get("component", "").strip(),
        graph_scope=request.GET.get("scope", "administrative").strip(),
        max_nodes=_integer(request, "max_nodes", 500) or 500,
        max_edges=_integer(request, "max_edges", 2_000) or 2_000,
    )


def _error(error: Exception, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": str(error), "read_only": True}, status=status)


@require_GET
def api_summary(request):
    return JsonResponse(get_explorer_service().get_graph_summary())


@require_GET
def api_full_graph(request):
    try:
        payload = get_explorer_service().get_full_graph(_filters(request))
    except ExplorerValidationError as error:
        return _error(error)
    response = JsonResponse(payload)
    if request.GET.get("download") == "1":
        response["Content-Disposition"] = 'attachment; filename="knowledge-graph-filtered.json"'
    return response


@require_GET
def api_neighborhood(request, document_id: int):
    try:
        depth = _integer(request, "depth", 1) or 1
        payload = get_explorer_service().get_document_neighborhood(
            document_id, direction=request.GET.get("direction", "both"), depth=depth,
            filters=_filters(request),
        )
    except ExplorerValidationError as error:
        return _error(error)
    except KeyError:
        return _error(Http404("Document node not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_search(request):
    query = request.GET.get("q", "").strip()[:300]
    try:
        limit = _integer(request, "limit", 25) or 25
        payload = get_explorer_service().search_graph_nodes(query, filters=_filters(request), limit=limit)
    except ExplorerValidationError as error:
        return _error(error)
    return JsonResponse(payload)


@require_GET
def api_node_detail(request, node_id: str):
    try:
        payload = get_explorer_service().get_node_details(node_id)
    except ExplorerValidationError as error:
        return _error(error)
    if payload is None:
        return _error(Http404("Graph node not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_edge_detail(request, relation_id: int):
    try:
        payload = get_explorer_service().get_edge_details(relation_id)
    except ExplorerValidationError as error:
        return _error(error)
    if payload is None:
        return _error(Http404("Validated graph relation not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_components(request):
    return JsonResponse(get_explorer_service().get_components())


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
