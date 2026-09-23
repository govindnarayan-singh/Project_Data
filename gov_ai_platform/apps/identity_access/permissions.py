from __future__ import annotations

from functools import wraps

from django.http import HttpRequest, HttpResponseForbidden
from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import AccessGrant, AccessGrantState, AccessRole, IASAuthorityScope


def effective_grants(user, *, role: str | None = None) -> QuerySet[AccessGrant]:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return AccessGrant.objects.none()
    now = timezone.now()
    grants = AccessGrant.objects.filter(
        user=user,
        state=AccessGrantState.ACTIVE,
        starts_at__lte=now,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    return grants.filter(role=role) if role else grants


def has_application_access(user) -> bool:
    return effective_grants(user).exists()


def is_ias_officer(user) -> bool:
    return effective_grants(user, role=AccessRole.IAS_OFFICER).exists()


def primary_access_role(user) -> str:
    if is_ias_officer(user):
        return AccessRole.IAS_OFFICER
    if effective_grants(user, role=AccessRole.OFFICER).exists():
        return AccessRole.OFFICER
    return ""


def authorized_organization_ids(user) -> set[int]:
    if not is_ias_officer(user):
        return set()
    now = timezone.now()
    return set(
        IASAuthorityScope.objects.filter(user=user, is_active=True)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .values_list("organization_id", flat=True)
    )


def can_manage_organization(user, organization_id: int) -> bool:
    return organization_id in authorized_organization_ids(user)


def can_review_access_requests(user) -> bool:
    return bool(authorized_organization_ids(user))


def ias_required(view):
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs):
        if not can_review_access_requests(request.user):
            return HttpResponseForbidden("IAS authorization and organizational scope are required.")
        return view(request, *args, **kwargs)

    return wrapped


# Future workflow hooks fail closed until their stages define object-level rules.
def can_view_unresolved_reference(user, reference) -> bool:
    return False


def can_submit_document(user, scope) -> bool:
    return False


def can_approve_document(user, scope) -> bool:
    return False


def can_review_candidate(user, scope) -> bool:
    return False


def can_approve_review(user, scope) -> bool:
    return False
