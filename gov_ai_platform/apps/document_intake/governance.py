from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.governance.audit import record_governance_event
from apps.governance.constants import ApprovalDecisionType, AuditAction, AuditCategory, AuditOutcome
from apps.governance.exceptions import GovernanceApprovalError
from apps.governance.policies import ApprovalPolicy, SubjectSnapshot, get_policy, register_policy
from apps.identity_access.models import AccessRole
from apps.identity_access.permissions import can_approve_document

from .models import (
    DocumentIngestionJob,
    DocumentSubmission,
    DuplicateStatus,
    JobState,
    SecurityStatus,
    SubmissionState,
    ValidationState,
)


WORKFLOW = "DOCUMENT_CORPUS_ADMISSION"
SUBJECT_TYPE = "document_intake.DocumentSubmission"


def read_document_subject(subject_id: str) -> SubjectSnapshot:
    try:
        submission = DocumentSubmission.objects.select_for_update().get(pk=subject_id)
    except (DocumentSubmission.DoesNotExist, ValueError) as exc:
        raise GovernanceApprovalError("SUBJECT_NOT_FOUND", "Document submission not found.") from exc
    return SubjectSnapshot(
        version=submission.version,
        organization_id=submission.organization_id,
        digest=submission.sha256,
        state=submission.state,
    )


def prohibit_document_self_approval(actor, case) -> bool:
    if case.submitted_by_id != getattr(actor, "pk", None):
        return False
    try:
        role = case.document_submission.submitter_role_snapshot
    except DocumentSubmission.DoesNotExist:
        return True
    if role == AccessRole.OFFICER:
        return True
    return not settings.DOCUMENT_IAS_SELF_CONFIRM_ALLOWED


def apply_document_decision(*, actor, case, decision, reason, snapshot, correlation_id) -> None:
    submission = DocumentSubmission.objects.select_for_update().get(approval_case=case)
    if submission.version != snapshot.version or submission.state != SubmissionState.PENDING_IAS_APPROVAL:
        raise GovernanceApprovalError("STALE_SUBJECT", "The submission changed; refresh before deciding.")
    if submission.validation_state != ValidationState.PASSED or submission.security_status != SecurityStatus.PASSED:
        raise GovernanceApprovalError("SUBJECT_NOT_ELIGIBLE", "The submission did not pass validation.")
    if submission.duplicate_status in {DuplicateStatus.EXACT_PENDING_DUPLICATE, DuplicateStatus.EXACT_CORPUS_DUPLICATE}:
        raise GovernanceApprovalError("DUPLICATE_NOT_ELIGIBLE", "Exact duplicate bytes cannot be admitted again.")
    correlation = submission.correlation_id
    self_confirmation = submission.submitted_by_id == actor.pk
    if decision == ApprovalDecisionType.REJECT:
        previous = submission.state
        submission.state = SubmissionState.REJECTED
        submission.safe_failure_code = "APPROVAL_REJECTED"
        submission.safe_failure_reason = "The IAS reviewer rejected corpus admission."
        submission.version += 1
        submission.save(update_fields=("state", "safe_failure_code", "safe_failure_reason", "version", "updated_at"))
        record_governance_event(
            category=AuditCategory.DOCUMENT,
            action=AuditAction.DOCUMENT_REJECTED,
            outcome=AuditOutcome.SUCCESS,
            actor=actor,
            actor_role=AccessRole.IAS_OFFICER,
            organization_id=submission.organization_id,
            resource_type=SUBJECT_TYPE,
            resource_id=str(submission.pk),
            resource_version=submission.version,
            correlation_id=correlation,
            reason_code="APPROVAL_REJECTED",
            reason=reason,
            previous_state=previous,
            new_state=submission.state,
            metadata={"self_confirmation": self_confirmation},
        )
        return

    previous = submission.state
    submission.state = SubmissionState.QUEUED_FOR_INGESTION
    submission.safe_failure_code = ""
    submission.safe_failure_reason = ""
    submission.version += 1
    submission.save(update_fields=("state", "safe_failure_code", "safe_failure_reason", "version", "updated_at"))
    record_governance_event(
        category=AuditCategory.DOCUMENT,
        action=AuditAction.DOCUMENT_APPROVED,
        outcome=AuditOutcome.SUCCESS,
        actor=actor,
        actor_role=AccessRole.IAS_OFFICER,
        organization_id=submission.organization_id,
        resource_type=SUBJECT_TYPE,
        resource_id=str(submission.pk),
        resource_version=submission.version,
        correlation_id=correlation,
        reason_code="CORPUS_ADMISSION_APPROVED",
        reason=reason,
        previous_state=previous,
        new_state="APPROVED",
        metadata={"self_confirmation": self_confirmation, "source_provenance": submission.source_provenance},
    )
    job = DocumentIngestionJob.objects.create(
        submission=submission,
        state=JobState.PENDING,
        max_attempts=settings.DOCUMENT_INGESTION_MAX_ATTEMPTS,
        idempotency_key=f"document-admission:{submission.pk}:{submission.sha256}",
    )
    record_governance_event(
        category=AuditCategory.DOCUMENT,
        action=AuditAction.DOCUMENT_INGESTION_QUEUED,
        outcome=AuditOutcome.SUCCESS,
        actor=actor,
        actor_role=AccessRole.IAS_OFFICER,
        organization_id=submission.organization_id,
        resource_type="document_intake.DocumentIngestionJob",
        resource_id=str(job.pk),
        resource_version=job.version,
        correlation_id=correlation,
        reason_code="APPROVAL_COMMITTED",
        previous_state="APPROVED",
        new_state=JobState.PENDING,
        metadata={"ingestion_attempt": 0},
    )


def register_document_admission_policy() -> None:
    try:
        get_policy(WORKFLOW)
        return
    except GovernanceApprovalError as exc:
        if exc.code != "UNKNOWN_WORKFLOW":
            raise
    register_policy(ApprovalPolicy(
        workflow_type=WORKFLOW,
        required_reviewer_role=AccessRole.IAS_OFFICER,
        prohibit_self_approval=prohibit_document_self_approval,
        read_subject=read_document_subject,
        can_review_organization=can_approve_document,
        allowed_decisions=frozenset({ApprovalDecisionType.APPROVE, ApprovalDecisionType.REJECT}),
        apply_decision=apply_document_decision,
    ))
