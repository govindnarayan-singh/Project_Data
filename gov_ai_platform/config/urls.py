from django.urls import include, path


urlpatterns = [
    path("access/", include("apps.identity_access.urls")),
    path("governance/", include("apps.governance.urls")),
    path("document-intake/", include("apps.document_intake.urls")),
    path("", include("apps.search_ui.urls")),
    path("documents/", include("apps.documents.urls")),
    path("knowledge-graph/", include("apps.knowledge_graph_ui.urls")),
    path("system/", include("apps.system_ui.urls")),
]
