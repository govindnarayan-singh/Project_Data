from __future__ import annotations

from .audit import new_correlation_id


class CorrelationIdMiddleware:
    """Assign a server-generated request UUID and expose it as X-Request-ID."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = new_correlation_id()
        response = self.get_response(request)
        response["X-Request-ID"] = str(request.correlation_id)
        return response
