from django.urls import path

from .views import (
    api_components,
    api_edge_detail,
    api_full_graph,
    api_neighborhood,
    api_node_detail,
    api_search,
    api_summary,
    overview,
    unresolved_detail,
    unresolved_list,
)

app_name = "knowledge_graph_ui"
urlpatterns = [
    path("", overview, name="overview"),
    path("api/summary/", api_summary, name="api_summary"),
    path("api/graph/", api_full_graph, name="api_graph"),
    path("api/neighborhood/<int:document_id>/", api_neighborhood, name="api_neighborhood"),
    path("api/search/", api_search, name="api_search"),
    path("api/nodes/<str:node_id>/", api_node_detail, name="api_node_detail"),
    path("api/edges/<int:relation_id>/", api_edge_detail, name="api_edge_detail"),
    path("api/components/", api_components, name="api_components"),
    path("unresolved/", unresolved_list, name="unresolved"),
    path("unresolved/<int:candidate_id>/", unresolved_detail, name="unresolved_detail"),
]
