from django.db import models


GENESIS_HASH = "0" * 64
AUDIT_EVENT_SCHEMA_VERSION = 1


class ApprovalCaseState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class ApprovalDecisionType(models.TextChoices):
    APPROVE = "APPROVE", "Approve"
    REJECT = "REJECT", "Reject"


class AuditCategory(models.TextChoices):
    IDENTITY_ACCESS = "IDENTITY_ACCESS", "Identity and access"
    APPROVAL = "APPROVAL", "Approval"
    AUTHORIZATION = "AUTHORIZATION", "Authorization"
    SECURITY = "SECURITY", "Security"
    SYSTEM = "SYSTEM", "System"
    DOCUMENT = "DOCUMENT", "Document governance"


class AuditOutcome(models.TextChoices):
    SUCCESS = "SUCCESS", "Success"
    DENIED = "DENIED", "Denied"
    FAILURE = "FAILURE", "Failure"


class AuditOrigin(models.TextChoices):
    LIVE = "LIVE", "Recorded live"
    HISTORICAL_STAGE1_IMPORT = "HISTORICAL_STAGE1_IMPORT", "Historical Stage 1 import"


class AuditAction(models.TextChoices):
    ACCESS_REQUEST_SUBMITTED = "ACCESS_REQUEST_SUBMITTED", "Access request submitted"
    ACCESS_REQUEST_APPROVED = "ACCESS_REQUEST_APPROVED", "Access request approved"
    ACCESS_REQUEST_REJECTED = "ACCESS_REQUEST_REJECTED", "Access request rejected"
    ACCESS_GRANT_CREATED = "ACCESS_GRANT_CREATED", "Access grant created"
    ACCESS_GRANT_REACTIVATED = "ACCESS_GRANT_REACTIVATED", "Access grant reactivated"
    ACCESS_GRANT_SUSPENDED = "ACCESS_GRANT_SUSPENDED", "Access grant suspended"
    ACCESS_GRANT_REVOKED = "ACCESS_GRANT_REVOKED", "Access grant revoked"
    IAS_BOOTSTRAPPED = "IAS_BOOTSTRAPPED", "IAS identity bootstrapped"
    ACTIVATION_CONSUMED = "ACTIVATION_CONSUMED", "Activation consumed"
    APPROVAL_CASE_CREATED = "APPROVAL_CASE_CREATED", "Approval case created"
    APPROVAL_CASE_APPROVED = "APPROVAL_CASE_APPROVED", "Approval case approved"
    APPROVAL_CASE_REJECTED = "APPROVAL_CASE_REJECTED", "Approval case rejected"
    ACCESS_REQUEST_STATE_IMPORTED = "ACCESS_REQUEST_STATE_IMPORTED", "Access request history imported"
    DOCUMENT_SUBMITTED = "DOCUMENT_SUBMITTED", "Document submitted"
    DOCUMENT_REVALIDATION_REQUESTED = "DOCUMENT_REVALIDATION_REQUESTED", "Document revalidation requested"
    DOCUMENT_VALIDATION_PASSED = "DOCUMENT_VALIDATION_PASSED", "Document validation passed"
    DOCUMENT_VALIDATION_FAILED = "DOCUMENT_VALIDATION_FAILED", "Document validation failed"
    DOCUMENT_READY_FOR_APPROVAL = "DOCUMENT_READY_FOR_APPROVAL", "Document ready for approval"
    DOCUMENT_APPROVED = "DOCUMENT_APPROVED", "Document approved"
    DOCUMENT_REJECTED = "DOCUMENT_REJECTED", "Document rejected"
    DOCUMENT_INGESTION_QUEUED = "DOCUMENT_INGESTION_QUEUED", "Document ingestion queued"
    DOCUMENT_INGESTION_STARTED = "DOCUMENT_INGESTION_STARTED", "Document ingestion started"
    DOCUMENT_INGESTION_RETRIED = "DOCUMENT_INGESTION_RETRIED", "Document ingestion retried"
    DOCUMENT_INGESTION_RETRY_REQUESTED = "DOCUMENT_INGESTION_RETRY_REQUESTED", "Document ingestion retry requested"
    DOCUMENT_INGESTION_SUCCEEDED = "DOCUMENT_INGESTION_SUCCEEDED", "Document ingestion succeeded"
    DOCUMENT_INGESTION_FAILED = "DOCUMENT_INGESTION_FAILED", "Document ingestion failed"
    UNRESOLVED_WORK_ITEM_CLAIMED = "UNRESOLVED_WORK_ITEM_CLAIMED", "Unresolved work item claimed"
    UNRESOLVED_WORK_ITEM_RELEASED = "UNRESOLVED_WORK_ITEM_RELEASED", "Unresolved work item released"
    UNRESOLVED_WORK_ITEM_ASSIGNED = "UNRESOLVED_WORK_ITEM_ASSIGNED", "Unresolved work item assigned"
    UNRESOLVED_DOCUMENT_SUBMITTED = "UNRESOLVED_DOCUMENT_SUBMITTED", "Unresolved document submitted"
    REFERENCE_RESOLUTION_PROPOSED = "REFERENCE_RESOLUTION_PROPOSED", "Reference resolution proposed"
    REFERENCE_RESOLUTION_APPROVED = "REFERENCE_RESOLUTION_APPROVED", "Reference resolution approved"
    REFERENCE_RESOLUTION_REJECTED = "REFERENCE_RESOLUTION_REJECTED", "Reference resolution rejected"
    REFERENCE_RESOLVED_AUTO = "REFERENCE_RESOLVED_AUTO", "Reference resolved automatically"
    WORK_ITEM_CLOSED_INVALID = "WORK_ITEM_CLOSED_INVALID", "Work item closed as invalid"


ALLOWED_AUDIT_METADATA_KEYS = frozenset({
    "decision",
    "grant_id",
    "grant_role",
    "grant_state",
    "historical_transition_version",
    "risk_flags",
    "subject_digest_present",
    "validation_outcome",
    "duplicate_status",
    "source_provenance",
    "page_count",
    "byte_size",
    "self_confirmation",
    "failure_code",
    "ingestion_attempt",
    "validation_attempt",
    "validator_version",
    "signature_status",
    "candidate_count",
    "target_document_id",
    "relation_ids",
    "submission_id",
    "assignee_id",
    "claim_expires_at",
    "resolution_method",
    "proposal_id",
    "local_catalog_id",
})
