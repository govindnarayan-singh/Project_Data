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


ALLOWED_AUDIT_METADATA_KEYS = frozenset({
    "decision",
    "grant_id",
    "grant_role",
    "grant_state",
    "historical_transition_version",
    "risk_flags",
    "subject_digest_present",
    "validation_outcome",
})
