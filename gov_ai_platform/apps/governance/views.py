from __future__ import annotations

from datetime import datetime, time

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from apps.document_intake.models import DocumentSubmission
from apps.document_intake.presentation import failure_message_for_code
from apps.identity_access.permissions import authorized_organization_ids, ias_required
from apps.knowledge_graph_ui.models import ReferenceResolutionProposal

from .constants import ApprovalCaseState, AuditAction, AuditCategory, AuditOutcome
from .models import GovernanceApprovalCase, GovernanceAuditEvent


RESOURCE_LABELS = {
    "identity_access.AccessRequest": "Access request",
    "identity_access.AccessGrant": "Access grant",
    "governance.GovernanceApprovalCase": "Approval case",
    "document_intake.DocumentSubmission": "Document submission",
    "document_intake.DocumentIngestionJob": "Document ingestion job",
    "knowledge_graph_ui.UnresolvedReferenceWorkItem": "Unresolved reference work item",
    "knowledge_graph_ui.ReferenceResolutionProposal": "Reference resolution proposal",
}


def _decorate_audit_events(events):
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    usernames = dict(get_user_model().objects.filter(pk__in=actor_ids).values_list("pk", "username"))
    for event in events:
        event.display_actor = usernames.get(event.actor_user_id, "System")
        event.display_action = event.get_action_display()
        event.display_category = event.get_category_display()
        event.display_outcome = event.get_outcome_display()
        event.display_reason_summary = (
            failure_message_for_code(event.reason_code, event.reason).explanation
            if event.action == AuditAction.DOCUMENT_VALIDATION_FAILED
            else event.reason
        )
        event.display_resource = RESOURCE_LABELS.get(
            event.resource_type,
            event.resource_type.rsplit(".", 1)[-1].replace("_", " ").title(),
        )
        event.short_resource_id = str(event.resource_id)[:12] + ("…" if len(str(event.resource_id)) > 12 else "")


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
    _decorate_audit_events(page.object_list)
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
    _decorate_audit_events([event])
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
    document_subjects = {
        str(submission.approval_case_id): submission
        for submission in DocumentSubmission.objects.filter(
            approval_case_id__in=[case.pk for case in page.object_list]
        )
    }
    resolution_subjects = {
        str(proposal.approval_case_id): proposal
        for proposal in ReferenceResolutionProposal.objects.filter(
            approval_case_id__in=[case.pk for case in page.object_list]
        ).select_related("work_item")
    }
    for case in page.object_list:
        submission = document_subjects.get(str(case.pk))
        proposal = resolution_subjects.get(str(case.pk))
        case.display_workflow = (
            "Document corpus admission"
            if case.workflow_type == "DOCUMENT_CORPUS_ADMISSION"
            else "Knowledge-graph reference resolution"
            if case.workflow_type == "KG_REFERENCE_RESOLUTION"
            else case.workflow_type.replace("_", " ").title()
        )
        case.display_subject = (
            str(submission.detected_metadata.get("subject") or submission.original_filename)
            if submission
            else f"{proposal.work_item.display_identifier} → corpus document {proposal.target_document_id}"
            if proposal
            else f"{case.subject_type.rsplit('.', 1)[-1]} {str(case.subject_id)[:12]}"
        )
        case.detail_url = (
            reverse("document_intake:detail", args=[submission.pk])
            if submission
            else reverse("knowledge_graph_ui:work_item_detail", args=[proposal.work_item_id])
            if proposal
            else ""
        )
    return render(request, "governance/approval_case_list.html", {
        "page_obj": page,
        "states": ApprovalCaseState.choices,
        "selected_state": state,
        "active_section": "governance-approvals",
    })
