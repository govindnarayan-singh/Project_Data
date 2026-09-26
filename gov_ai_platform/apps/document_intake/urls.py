from django.urls import path

from . import views


app_name = "document_intake"
urlpatterns = [
    path("", views.submission_list, name="list"),
    path("submit/", views.submit, name="submit"),
    path("<uuid:submission_id>/", views.detail, name="detail"),
    path("<uuid:submission_id>/preview/", views.preview, name="preview"),
    path("<uuid:submission_id>/download/", views.download, name="download"),
    path("<uuid:submission_id>/decision/", views.decide, name="decide"),
    path("<uuid:submission_id>/retry/", views.retry, name="retry"),
]
