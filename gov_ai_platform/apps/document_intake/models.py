from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class SubmissionState(models.TextChoices):
    VALIDATION_FAILED = "VALIDATION_FAILED", "Validation failed"
    PENDING_IAS_APPROVAL = "PENDING_IAS_APPROVAL", "Pending IAS approval"
    REJECTED = "REJECTED", "Rejected"
    QUEUED_FOR_INGESTION = "QUEUED_FOR_INGESTION", "Approved — queued for ingestion"
    INGESTING = "INGESTING", "Approved — ingesting"
    INGESTED = "INGESTED", "Ingested"
    INGESTION_FAILED = "INGESTION_FAILED", "Approved — ingestion failed"


class ValidationState(models.TextChoices):
    PASSED = "PASSED", "Passed"
    FAILED = "FAILED", "Failed"


class SecurityStatus(models.TextChoices):
    PASSED = "PASSED", "Structural validation passed"
    FAILED = "FAILED", "Security validation failed"


class MalwareStatus(models.TextChoices):
    NOT_PERFORMED = "NOT_PERFORMED", "Not performed"
    CLEAN = "CLEAN", "Scanner reported clean"
    MALWARE_SCAN_NOT_CONFIGURED = "MALWARE_SCAN_NOT_CONFIGURED", "Malware scanner not configured"
    MALICIOUS = "MALICIOUS", "Malicious content detected"
    SCAN_ERROR = "SCAN_ERROR", "Malware scan failed"


class SourceProvenance(models.TextChoices):
    AUTHORITATIVE_SOURCE_VERIFIED = "AUTHORITATIVE_SOURCE_VERIFIED", "Authoritative source independently verified"
    IAS_ATTESTED_OFFICIAL_COPY = "IAS_ATTESTED_OFFICIAL_COPY", "IAS-attested official-source copy"
    IAS_VERIFIED_COPY = "IAS_VERIFIED_COPY", "Legacy IAS-verified copy"
    OFFICER_PROVIDED_UNVERIFIED = "OFFICER_PROVIDED_UNVERIFIED", "Officer-provided, unverified"
    UNKNOWN = "UNKNOWN", "Unknown"


class SignatureStatus(models.TextChoices):
    NOT_CHECKED = "NOT_CHECKED", "Not checked"
    NOT_PRESENT = "NOT_PRESENT", "Not present"
    PRESENT_UNVERIFIED = "SIGNATURE_PRESENT_UNVERIFIED", "Present — cryptographic verification not performed"
    VERIFIED_VALID = "SIGNATURE_VERIFIED_VALID", "Cryptographically verified"
    VERIFICATION_FAILED = "SIGNATURE_VERIFICATION_FAILED", "Cryptographic verification failed"
    VERIFICATION_UNAVAILABLE = "SIGNATURE_VERIFICATION_UNAVAILABLE", "Verification unavailable"


class DuplicateStatus(models.TextChoices):
    NEW = "NEW", "New"
    EXACT_PENDING_DUPLICATE = "EXACT_PENDING_DUPLICATE", "Exact pending duplicate"
    EXACT_CORPUS_DUPLICATE = "EXACT_CORPUS_DUPLICATE", "Exact corpus duplicate"
    POSSIBLE_IDENTITY_DUPLICATE = "POSSIBLE_IDENTITY_DUPLICATE", "Possible identity duplicate"


class SubmissionOrigin(models.TextChoices):
    DIRECT_UPLOAD = "DIRECT_UPLOAD", "Direct upload"
    UNRESOLVED_REFERENCE = "UNRESOLVED_REFERENCE", "Unresolved reference"
    QUERY_MISSING_REFERENCE = "QUERY_MISSING_REFERENCE", "Query missing reference"


class JobState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    RETRYABLE = "RETRYABLE", "Retryable"
    FAILED = "FAILED", "Failed"


ACTIVE_DIGEST_STATES = (
    SubmissionState.PENDING_IAS_APPROVAL,
    SubmissionState.QUEUED_FOR_INGESTION,
    SubmissionState.INGESTING,
    SubmissionState.INGESTED,
)


class DocumentSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("identity_access.OrganizationUnit", on_delete=models.PROTECT, related_name="document_submissions")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="document_submissions")
    submitter_role_snapshot = models.CharField(max_length=32)
    original_filename = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=255, unique=True)
    sha256 = models.CharField(max_length=64, db_index=True)
    byte_size = models.PositiveBigIntegerField()
    declared_mime = models.CharField(max_length=128)
    detected_mime = models.CharField(max_length=128, default="application/pdf")
    state = models.CharField(max_length=32, choices=SubmissionState.choices)
    validation_state = models.CharField(max_length=16, choices=ValidationState.choices)
    security_status = models.CharField(max_length=16, choices=SecurityStatus.choices)
    malware_status = models.CharField(max_length=40, choices=MalwareStatus.choices)
    source_provenance = models.CharField(max_length=48, choices=SourceProvenance.choices)
    claimed_source_url = models.URLField(max_length=1000, blank=True)
    claimed_identifier = models.CharField(max_length=255, blank=True)
    submission_reason = models.CharField(max_length=1000)
    detected_metadata = models.JSONField(default=dict, blank=True)
    duplicate_status = models.CharField(max_length=40, choices=DuplicateStatus.choices, default=DuplicateStatus.NEW)
    matched_submission = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicate_submissions")
    matched_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    approval_case = models.OneToOneField("governance.GovernanceApprovalCase", null=True, blank=True, on_delete=models.SET_NULL, related_name="document_submission")
    archive_key = models.CharField(max_length=255, blank=True)
    ingested_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extraction_status = models.CharField(max_length=40, blank=True)
    preview_text = models.TextField(blank=True)
    validation_findings = models.JSONField(default=list, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    validator_version = models.CharField(max_length=64, blank=True)
    signature_status = models.CharField(
        max_length=48,
        choices=SignatureStatus.choices,
        default=SignatureStatus.NOT_CHECKED,
    )
    signature_summary = models.JSONField(default=list, blank=True)
    external_uri_domains = models.JSONField(default=list, blank=True)
    source_context = models.CharField(max_length=1000, blank=True)
    ias_source_attested = models.BooleanField(default=False)
    origin = models.CharField(max_length=32, choices=SubmissionOrigin.choices, default=SubmissionOrigin.DIRECT_UPLOAD)
    origin_reference = models.CharField(max_length=128, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    safe_failure_code = models.CharField(max_length=64, blank=True)
    safe_failure_reason = models.CharField(max_length=500, blank=True)
    internal_failure_detail = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(state__in=SubmissionState.values), name="doc_submission_state_valid"),
            models.CheckConstraint(condition=Q(validation_state__in=ValidationState.values), name="doc_validation_state_valid"),
            models.CheckConstraint(condition=Q(security_status__in=SecurityStatus.values), name="doc_security_state_valid"),
            models.CheckConstraint(condition=Q(duplicate_status__in=DuplicateStatus.values), name="doc_duplicate_state_valid"),
            models.UniqueConstraint(fields=("sha256",), condition=Q(state__in=ACTIVE_DIGEST_STATES), name="doc_one_active_sha256"),
        ]
        indexes = [
            models.Index(fields=("organization", "state", "created_at"), name="doc_scope_state_idx"),
            models.Index(fields=("submitted_by", "created_at"), name="doc_submitter_time_idx"),
        ]


class AppendOnlyValidationAttemptQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Validation attempts are append-only.")

    def delete(self):
        raise ValueError("Validation attempts are append-only.")


class DocumentValidationAttempt(models.Model):
    submission = models.ForeignKey(
        DocumentSubmission,
        on_delete=models.PROTECT,
        related_name="validation_attempts",
    )
    attempt_number = models.PositiveIntegerField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="document_validation_attempts",
    )
    validator_version = models.CharField(max_length=64)
    result_code = models.CharField(max_length=64)
    validation_state = models.CharField(max_length=16, choices=ValidationState.choices)
    security_status = models.CharField(max_length=16, choices=SecurityStatus.choices)
    malware_status = models.CharField(max_length=40, choices=MalwareStatus.choices)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extraction_status = models.CharField(max_length=40, blank=True)
    findings = models.JSONField(default=list, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    signature_status = models.CharField(max_length=48, choices=SignatureStatus.choices)
    signature_summary = models.JSONField(default=list, blank=True)
    external_uri_domains = models.JSONField(default=list, blank=True)
    safe_failure_code = models.CharField(max_length=64, blank=True)
    safe_failure_reason = models.CharField(max_length=500, blank=True)
    internal_failure_detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyValidationAttemptQuerySet.as_manager()

    class Meta:
        ordering = ("submission_id", "attempt_number")
        constraints = [
            models.UniqueConstraint(
                fields=("submission", "attempt_number"),
                name="doc_validation_attempt_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("submission", "created_at"), name="doc_validation_time_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Validation attempts are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Validation attempts are append-only.")


class DocumentIngestionJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(DocumentSubmission, on_delete=models.PROTECT, related_name="ingestion_job")
    state = models.CharField(max_length=16, choices=JobState.choices, default=JobState.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    idempotency_key = models.CharField(max_length=128, unique=True)
    worker_id = models.CharField(max_length=128, blank=True)
    current_stage = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ingestion_run_id = models.CharField(max_length=64, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    safe_error_reason = models.CharField(max_length=500, blank=True)
    internal_error_detail = models.TextField(blank=True)
    kg_result = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [models.CheckConstraint(condition=Q(state__in=JobState.values), name="doc_job_state_valid")]
        indexes = [models.Index(fields=("state", "created_at"), name="doc_job_claim_idx")]


class DocumentIngestionWorkerHeartbeat(models.Model):
    worker_id = models.CharField(max_length=128, primary_key=True)
    started_at = models.DateTimeField()
    last_heartbeat = models.DateTimeField()
    current_job = models.ForeignKey(
        DocumentIngestionJob,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="worker_heartbeats",
    )

    class Meta:
        ordering = ("-last_heartbeat",)
        indexes = [models.Index(fields=("last_heartbeat",), name="doc_worker_heartbeat_idx")]
