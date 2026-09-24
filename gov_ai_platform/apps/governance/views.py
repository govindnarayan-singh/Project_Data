from __future__ import annotations

from datetime import datetime, time

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from apps.identity_access.permissions import authorized_organization_ids, ias_required

from .constants import ApprovalCaseState, AuditAction, AuditCategory, AuditOutcome
from .models import GovernanceApprovalCase, GovernanceAuditEvent


def _bounded_choice(value: str, allowed: set[str]) -> str:
    return value if value in allowed else ""


@require_GET
@ias_required
def audit_list(request):
    scope_ids = authorized_organization_ids(request.user)
    queryset = GovernanceAuditEvent.objects.filter(organization_id_snapshot__in=scope_ids)
    category = _bounded_choice(request.GET.get("category", ""), set(AuditCategory.values))
    action = _bounded_choice(request.GET.get("action", ""), set(AuditAction.values))
    outcome = _bounded_choice(request.GET.get("outcome", ""), set(AuditOutcome.values))
    resource_type = request.GET.get("resource_type", "").strip()[:64]
    actor = request.GET.get("actor", "").strip()
    date_from = parse_date(request.GET.get("date_from", ""))
    date_to = parse_date(request.GET.get("date_to", ""))
    if category:
        queryset = queryset.filter(category=category)
    if action:
        queryset = queryset.filter(action=action)
    if outcome:
        queryset = queryset.filter(outcome=outcome)
    if resource_type:
        queryset = queryset.filter(resource_type=resource_type)
    if actor.isdigit():
        queryset = queryset.filter(actor_user_id=int(actor))
    if date_from:
        queryset = queryset.filter(occurred_at__gte=timezone.make_aware(datetime.combine(date_from, time.min)))
    if date_to:
        queryset = queryset.filter(occurred_at__lte=timezone.make_aware(datetime.combine(date_to, time.max)))
    page = Paginator(queryset.order_by("-sequence"), 50).get_page(request.GET.get("page"))
    return render(request, "governance/audit_list.html", {
        "page_obj": page,
        "categories": AuditCategory.choices,
        "actions": AuditAction.choices,
        "outcomes": AuditOutcome.choices,
        "filters": {
            "category": category,
            "action": action,
            "outcome": outcome,
            "resource_type": resource_type,
            "actor": actor if actor.isdigit() else "",
            "date_from": request.GET.get("date_from", "") if date_from else "",
            "date_to": request.GET.get("date_to", "") if date_to else "",
        },
        "active_section": "governance-audit",
    })


@require_GET
@ias_required
def audit_detail(request, event_id):
    event = get_object_or_404(
        GovernanceAuditEvent,
        event_id=event_id,
        organization_id_snapshot__in=authorized_organization_ids(request.user),
    )
    return render(request, "governance/audit_detail.html", {"event": event, "active_section": "governance-audit"})


@require_GET
@ias_required
def approval_case_list(request):
    scope_ids = authorized_organization_ids(request.user)
    state = _bounded_choice(request.GET.get("state", ""), set(ApprovalCaseState.values))
    queryset = GovernanceApprovalCase.objects.filter(organization_id__in=scope_ids).select_related("organization", "submitted_by")
    if state:
        queryset = queryset.filter(state=state)
    page = Paginator(queryset.order_by("-created_at"), 50).get_page(request.GET.get("page"))
    return render(request, "governance/approval_case_list.html", {
        "page_obj": page,
        "states": ApprovalCaseState.choices,
        "selected_state": state,
        "active_section": "governance-approvals",
    })
