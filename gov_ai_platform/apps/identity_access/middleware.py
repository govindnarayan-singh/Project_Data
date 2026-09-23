from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from .permissions import has_application_access


PUBLIC_VIEW_NAMES = {
    "identity_access:login",
    "identity_access:logout",
    "identity_access:request_access",
    "identity_access:request_status",
    "identity_access:activate",
    "identity_access:access_denied",
}


class AccessControlMiddleware:
    """Deny protected application access unless a current grant exists."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        static_prefix = f"/{settings.STATIC_URL.lstrip('/')}"
        if path.startswith(static_prefix):
            return self.get_response(request)
        try:
            match = resolve(path)
            is_public = match.view_name in PUBLIC_VIEW_NAMES
        except Resolver404:
            is_public = False
        if is_public:
            return self.get_response(request)

        is_api = path.startswith("/knowledge-graph/api/")
        if not request.user.is_authenticated:
            if is_api:
                return JsonResponse({"error": "authentication_required"}, status=401)
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not has_application_access(request.user):
            if is_api:
                return JsonResponse({"error": "access_denied"}, status=403)
            return redirect(reverse("identity_access:access_denied"))
        return self.get_response(request)
