from __future__ import annotations

import re
import uuid

from django.db import transaction
from django.utils import timezone

from apps.identity_access.permissions import effective_grants

from .audit import record_governance_event
from .constants import (
    ApprovalCaseState,
    ApprovalDecisionType,
    AuditAction,
    AuditCategory,
    AuditOutcome,
)
from .exceptions import AuditError, GovernanceApprovalError
from .models import GovernanceApprovalCase, GovernanceApprovalDecision
from .policies import get_policy


CONTROLLED_SUBJECT_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")


def create_approval_case(
    *,
    workflow_type: str,
    organization,
    submitted_by,
    subject_type: str,
    subject_id: str,
    subject_version: int,
    subject_digest: str = "",
    expires_at=None,
    correlation_id: uuid.UUID | None = None,
) -> GovernanceApprovalCase:
    policy = get_policy(workflow_type)
    if not getattr(submitted_by, "is_authenticated", False) or not submitted_by.is_active:
        raise GovernanceApprovalError("UNAUTHENTICATED", "An authenticated active submitter is required.")
    if not CONTROLLED_SUBJECT_TYPE.fullmatch(subject_type) or not subject_id or len(str(subject_id)) > 128:
        raise GovernanceApprovalError("INVALID_SUBJECT", "The approval subject identifier is invalid.")
    if expires_at is not None and expires_at <= timezone.now():
        raise GovernanceApprovalError("INVALID_EXPIRY", "Approval case expiry must be in the future.")
    try:
        with transaction.atomic():
            # Policy adapters may use select_for_update() in read_subject().
            # Keeping the snapshot check inside this transaction makes that lock
            # meaningful through case creation and audit insertion.
            snapshot = policy.read_subject(str(subject_id))
            if snapshot.organization_id != organization.pk:
                raise GovernanceApprovalError("SUBJECT_SCOPE_MISMATCH", "The subject is outside the supplied organization.")
            if snapshot.version != subject_version or snapshot.digest != subject_digest:
                raise GovernanceApprovalError("STALE_SUBJECT", "The subject changed; refresh before submission.")
            case = GovernanceApprovalCase.objects.create(
                workflow_type=workflow_type,
                organization=organization,
                submitted_by=submitted_by,
                subject_type=subject_type,
                subject_id=str(subject_id),
                subject_version=subject_version,
                subject_digest=subject_digest,
                expires_at=expires_at,
            )
            record_governance_event(
                category=AuditCategory.APPROVAL,
                action=AuditAction.APPROVAL_CASE_CREATED,
                outcome=AuditOutcome.SUCCESS,
                actor=submitted_by,
                organization_id=organization.pk,
                resource_type="governance.GovernanceApprovalCase",
                resource_id=str(case.pk),
                resource_version=case.version,
                correlation_id=correlation_id,
                reason_code="CASE_SUBMITTED",
                new_state=case.state,
                metadata={"subject_digest_present": bool(subject_digest)},
            )
            return case
    except AuditError as exc:
        raise GovernanceApprovalError("AUDIT_FAILURE", "The approval case could not be created safely.") from exc


def decide_approval_case(
    actor,
    case_id,
    *,
    decision: str,
    reason: str,
    expected_case_version: int,
    expected_subject_version: int,
    correlation_id: uuid.UUID | None = None,
) -> GovernanceApprovalDecision:
    if decision not in ApprovalDecisionType.values:
        raise GovernanceApprovalError("INVALID_DECISION", "The requested decision is not supported.")
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > 1000:
        raise GovernanceApprovalError("INVALID_REASON", "A bounded decision reason is required.")
    try:
        with transaction.atomic():
            case = GovernanceApprovalCase.objects.select_for_update(of=("self",)).get(pk=case_id)
            policy = get_policy(case.workflow_type)
            if decision not in policy.allowed_decisions:
                raise GovernanceApprovalError("INVALID_DECISION", "This decision is not allowed by policy.")
            if not getattr(actor, "is_authenticated", False) or not actor.is_active:
                raise GovernanceApprovalError("UNAUTHENTICATED", "An authenticated active reviewer is required.")
            if not effective_grants(actor, role=policy.required_reviewer_role).exists():
                raise GovernanceApprovalError("WRONG_ROLE", "The reviewer role is not authorized for this workflow.")
            if not policy.can_review_organization(actor, case.organization_id):
                raise GovernanceApprovalError("OUT_OF_SCOPE", "The approval case is outside reviewer scope.")
            self_approval_prohibited = (
                policy.prohibit_self_approval(actor, case)
                if callable(policy.prohibit_self_approval)
                else policy.prohibit_self_approval
            )
            if self_approval_prohibited and case.submitted_by_id == actor.pk:
                raise GovernanceApprovalError("SELF_APPROVAL", "Maker-checker policy prohibits self-approval.")
            if case.state != ApprovalCaseState.PENDING:
                raise GovernanceApprovalError("NOT_PENDING", "This approval case already has a final state.")
            if case.expires_at is not None and case.expires_at <= timezone.now():
                raise GovernanceApprovalError("EXPIRED", "This approval case has expired.")
            if case.version != expected_case_version:
                raise GovernanceApprovalError("STALE_DECISION", "The approval case changed; refresh before deciding.")
            snapshot = policy.read_subject(case.subject_id)
            if (
                expected_subject_version != case.subject_version
                or snapshot.version != case.subject_version
                or snapshot.digest != case.subject_digest
                or snapshot.organization_id != case.organization_id
            ):
                raise GovernanceApprovalError("STALE_SUBJECT", "The subject changed; refresh before deciding.")

            old_state = case.state
            case.state = ApprovalCaseState.APPROVED if decision == ApprovalDecisionType.APPROVE else ApprovalCaseState.REJECTED
            case.version += 1
            case.save(update_fields=("state", "version", "updated_at"))
            decision_record = GovernanceApprovalDecision(
                case=case,
                actor=actor,
                actor_role_snapshot=policy.required_reviewer_role,
                organization_id_snapshot=case.organization_id,
                decision=decision,
                reason=clean_reason,
                subject_version_seen=expected_subject_version,
                case_version_seen=expected_case_version,
            )
            decision_record._governance_insert_allowed = True
            decision_record.save(force_insert=True)
            if policy.apply_decision is not None:
                policy.apply_decision(
                    actor=actor,
                    case=case,
                    decision=decision,
                    reason=clean_reason,
                    snapshot=snapshot,
                    correlation_id=correlation_id,
                )
            record_governance_event(
                category=AuditCategory.APPROVAL,
                action=(AuditAction.APPROVAL_CASE_APPROVED if decision == ApprovalDecisionType.APPROVE else AuditAction.APPROVAL_CASE_REJECTED),
                outcome=AuditOutcome.SUCCESS,
                actor=actor,
                actor_role=policy.required_reviewer_role,
                organization_id=case.organization_id,
                resource_type="governance.GovernanceApprovalCase",
                resource_id=str(case.pk),
                resource_version=case.version,
                correlation_id=correlation_id,
                reason_code="APPROVAL_DECISION",
                reason=clean_reason,
                previous_state=old_state,
                new_state=case.state,
                metadata={"decision": decision, "subject_digest_present": bool(case.subject_digest)},
            )
            return decision_record
    except GovernanceApprovalCase.DoesNotExist as exc:
        raise GovernanceApprovalError("CASE_NOT_FOUND", "Approval case not found.") from exc
    except AuditError as exc:
        raise GovernanceApprovalError("AUDIT_FAILURE", "The decision could not be recorded safely.") from exc
