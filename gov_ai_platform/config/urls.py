from django.urls import include, path


urlpatterns = [
    path("", include("apps.search_ui.urls")),
    path("documents/", include("apps.documents.urls")),
]
