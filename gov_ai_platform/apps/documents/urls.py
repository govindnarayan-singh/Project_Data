from django.urls import path

from .views import document_detail, document_list, document_pdf


app_name = "documents"
urlpatterns = [
    path("", document_list, name="list"),
    path("<int:document_id>/", document_detail, name="detail"),
    path("<int:document_id>/pdf/", document_pdf, name="pdf"),
]
