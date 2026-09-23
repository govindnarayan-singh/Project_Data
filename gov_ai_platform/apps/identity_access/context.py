from .permissions import can_review_access_requests, primary_access_role


def access_context(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"access_role": "", "can_review_access": False}
    return {
        "access_role": primary_access_role(user),
        "can_review_access": can_review_access_requests(user),
    }
