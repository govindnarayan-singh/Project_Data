from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from apps.governance.constants import ApprovalDecisionType, AuditAction
from apps.governance.exceptions import GovernanceApprovalError
from apps.governance.policies import ApprovalPolicy, SubjectSnapshot, get_policy, register_policy
from apps.identity_access.models import AccessRole
from apps.identity_access.permissions import can_approve_review

from .models import ProposalState, ReferenceResolutionProposal, UnresolvedReferenceWorkItem, WorkItemStatus
from .workbench_services import (
    WORKFLOW,
    WorkbenchError,
    _audit,
    _proposal_digest,
    _record_activity,
    confirm_human_resolution,
)


def read_resolution_subject(subject_id: str) -> SubjectSnapshot:
    try:
        proposal = ReferenceResolutionProposal.objects.select_for_update().select_related("work_item").get(pk=subject_id)
    except (ReferenceResolutionProposal.DoesNotExist, ValueError) as exc:
        raise GovernanceApprovalError("SUBJECT_NOT_FOUND", "Reference-resolution proposal not found.") from exc
    return SubjectSnapshot(
        version=proposal.version,
        organization_id=proposal.work_item.organization_id,
        digest=_proposal_digest(proposal),
        state=proposal.state,
    )


def prohibit_resolution_self_approval(actor, case) -> bool:
    if case.submitted_by_id != getattr(actor, "pk", None):
        return False
    return not settings.KG_REFERENCE_IAS_SELF_CONFIRM_ALLOWED


def apply_resolution_decision(*, actor, case, decision, reason, snapshot, correlation_id) -> None:
    proposal = ReferenceResolutionProposal.objects.select_for_update().select_related("work_item").get(approval_case=case)
    if proposal.version != snapshot.version or proposal.state != ProposalState.PENDING:
        raise GovernanceApprovalError("STALE_SUBJECT", "The proposal changed; refresh before deciding.")
    item = UnresolvedReferenceWorkItem.objects.select_for_update().get(pk=proposal.work_item_id)
    previous = item.status
    now = timezone.now()
    if decision == ApprovalDecisionType.REJECT:
        proposal.state = ProposalState.REJECTED
        proposal.decided_by = actor
        proposal.decided_at = now
        proposal.decision_reason = reason
        proposal.version += 1
        proposal.save()
        item.status = WorkItemStatus.CLAIMED if item.claimed_by_id and item.claim_expires_at and item.claim_expires_at > now else WorkItemStatus.OPEN
        item.version += 1
        item.save(update_fields=("status", "version", "updated_at"))
        _record_activity(item, AuditAction.REFERENCE_RESOLUTION_REJECTED, "IAS rejected the resolution proposal.", actor=actor)
        _audit(
            item,
            AuditAction.REFERENCE_RESOLUTION_REJECTED,
            "REFERENCE_RESOLUTION_REJECTED",
            actor=actor,
            reason=reason,
            previous_state=previous,
            metadata={"proposal_id": str(proposal.pk), "target_document_id": proposal.target_document_id},
        )
        return
    try:
        relation_ids = confirm_human_resolution(proposal=proposal, reviewer=actor, reason=reason)
    except WorkbenchError as exc:
        raise GovernanceApprovalError(exc.code, exc.safe_message) from exc
    proposal.state = ProposalState.APPROVED
    proposal.decided_by = actor
    proposal.decided_at = now
    proposal.decision_reason = reason
    proposal.relation_ids = relation_ids
    proposal.version += 1
    proposal.save()
    item.status = WorkItemStatus.RESOLVED_HUMAN
    item.resolved_document_id = proposal.target_document_id
    item.resolution_method = f"GOVERNED_PROPOSAL:{proposal.pk}"
    item.resolution_reason = reason
    item.claimed_by = None
    item.claimed_at = None
    item.claim_expires_at = None
    item.version += 1
    item.save()
    _record_activity(item, AuditAction.REFERENCE_RESOLUTION_APPROVED, "IAS approved the resolution proposal.", actor=actor)
    _audit(
        item,
        AuditAction.REFERENCE_RESOLUTION_APPROVED,
        "REFERENCE_RESOLUTION_APPROVED",
        actor=actor,
        reason=reason,
        previous_state=previous,
        metadata={
            "proposal_id": str(proposal.pk),
            "target_document_id": proposal.target_document_id,
            "candidate_count": item.candidate_links.count(),
            "relation_ids": relation_ids[:20],
        },
    )


def register_reference_resolution_policy() -> None:
    try:
        get_policy(WORKFLOW)
        return
    except GovernanceApprovalError as exc:
        if exc.code != "UNKNOWN_WORKFLOW":
            raise
    register_policy(ApprovalPolicy(
        workflow_type=WORKFLOW,
        required_reviewer_role=AccessRole.IAS_OFFICER,
        prohibit_self_approval=prohibit_resolution_self_approval,
        read_subject=read_resolution_subject,
        can_review_organization=can_approve_review,
        allowed_decisions=frozenset({ApprovalDecisionType.APPROVE, ApprovalDecisionType.REJECT}),
        apply_decision=apply_resolution_decision,
    ))
