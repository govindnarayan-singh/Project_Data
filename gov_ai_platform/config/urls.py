from django.urls import include, path


urlpatterns = [
    path("", include("apps.search_ui.urls")),
    path("documents/", include("apps.documents.urls")),
    path("knowledge-graph/", include("apps.knowledge_graph_ui.urls")),
    path("system/", include("apps.system_ui.urls")),
]
