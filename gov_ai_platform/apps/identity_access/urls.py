from django.urls import path

from . import views


app_name = "identity_access"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("request/", views.request_access, name="request_access"),
    path("status/", views.request_status, name="request_status"),
    path("activate/<int:user_id>/<str:token>/", views.activate_account_view, name="activate"),
    path("denied/", views.access_denied, name="access_denied"),
    path("ias/requests/", views.ias_queue, name="ias_queue"),
    path("ias/requests/<uuid:request_id>/", views.ias_request_detail, name="ias_request_detail"),
    path("ias/requests/<uuid:request_id>/approve/", views.ias_approve_request, name="ias_approve_request"),
    path("ias/requests/<uuid:request_id>/reject/", views.ias_reject_request, name="ias_reject_request"),
    path("ias/members/", views.member_list, name="member_list"),
    path("ias/members/<int:grant_id>/state/", views.member_state_action, name="member_state_action"),
]
