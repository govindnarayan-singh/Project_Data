from django.urls import path

from .views import overview, unresolved_detail, unresolved_list

app_name = "knowledge_graph_ui"
urlpatterns = [
    path("", overview, name="overview"),
    path("unresolved/", unresolved_list, name="unresolved"),
    path("unresolved/<int:candidate_id>/", unresolved_detail, name="unresolved_detail"),
]
