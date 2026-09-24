from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.governance.audit import new_correlation_id, record_governance_event
from apps.governance.constants import AuditAction, AuditCategory, AuditOrigin, AuditOutcome
from apps.governance.models import GovernanceAuditEvent
from apps.identity_access.models import AccessRequestState, AccessRequestTransition


def _action_for_transition(new_state: str) -> str:
    if new_state == AccessRequestState.REQUESTED:
        return AuditAction.ACCESS_REQUEST_SUBMITTED
    if new_state == AccessRequestState.APPROVED:
        return AuditAction.ACCESS_REQUEST_APPROVED
    if new_state == AccessRequestState.REJECTED:
        return AuditAction.ACCESS_REQUEST_REJECTED
    return AuditAction.ACCESS_REQUEST_STATE_IMPORTED


class Command(BaseCommand):
    help = "Dry-run or explicitly import provenance-backed Stage 1 request transitions into the audit chain."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        transitions = AccessRequestTransition.objects.select_related("request").order_by("created_at", "id")
        # The first audit-row creation time is the Stage 2 cutover. Once a ledger
        # exists, later transitions are live-governance data and must never be
        # relabeled or duplicated as historical imports on a future replay.
        cutover = GovernanceAuditEvent.objects.order_by("created_at").values_list("created_at", flat=True).first()
        if cutover is not None:
            transitions = transitions.filter(created_at__lt=cutover)
        total = transitions.count()
        identifiers = [f"identity_access.transition:{pk}" for pk in transitions.values_list("pk", flat=True)]
        existing = set(
            GovernanceAuditEvent.objects.filter(source_identifier__in=identifiers)
            .values_list("source_identifier", flat=True)
        )
        pending = [item for item in transitions if f"identity_access.transition:{item.pk}" not in existing]
        apply = bool(options["apply"])
        imported = 0
        if apply:
            correlation_id = new_correlation_id()
            with transaction.atomic():
                for item in pending:
                    request = item.request
                    record_governance_event(
                        category=AuditCategory.IDENTITY_ACCESS,
                        action=_action_for_transition(item.new_state),
                        outcome=AuditOutcome.SUCCESS,
                        actor=item.actor,
                        organization_id=request.organization_id,
                        resource_type="identity_access.AccessRequest",
                        resource_id=str(request.pk),
                        resource_version=item.version,
                        correlation_id=correlation_id,
                        reason_code="HISTORICAL_STAGE1",
                        reason=item.reason,
                        previous_state=item.old_state,
                        new_state=item.new_state,
                        metadata={"historical_transition_version": item.version},
                        origin=AuditOrigin.HISTORICAL_STAGE1_IMPORT,
                        source_identifier=f"identity_access.transition:{item.pk}",
                        source_occurred_at=item.created_at,
                    )
                    imported += 1
        report = {
            "mode": "APPLY" if apply else "DRY_RUN",
            "cutover": cutover.isoformat() if cutover is not None else None,
            "provenance_source": "AccessRequestTransition",
            "transitions_found": total,
            "already_imported": len(existing),
            "eligible_to_import": len(pending),
            "imported": imported,
            "origin": AuditOrigin.HISTORICAL_STAGE1_IMPORT,
        }
        self.stdout.write(json.dumps(report, sort_keys=True))
