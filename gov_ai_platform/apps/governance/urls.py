from django.urls import path

from . import views


app_name = "governance"

urlpatterns = [
    path("audit/", views.audit_list, name="audit_list"),
    path("audit/<uuid:event_id>/", views.audit_detail, name="audit_detail"),
    path("approvals/", views.approval_case_list, name="approval_case_list"),
]
