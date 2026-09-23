from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class AccessRole(models.TextChoices):
    IAS_OFFICER = "IAS_OFFICER", "IAS officer"
    OFFICER = "OFFICER", "Officer"


class AccessGrantState(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    REVOKED = "REVOKED", "Revoked"
    EXPIRED = "EXPIRED", "Expired"


class AccessRequestState(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    PENDING_VALIDATION = "PENDING_VALIDATION", "Pending validation"
    PENDING_IAS_APPROVAL = "PENDING_IAS_APPROVAL", "Pending IAS approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"


class ValidationOutcome(models.TextChoices):
    ALLOW_TO_QUEUE = "ALLOW_TO_QUEUE", "Allowed to queue"
    REJECT_INVALID = "REJECT_INVALID", "Invalid"
    REJECT_DUPLICATE = "REJECT_DUPLICATE", "Duplicate"
    RATE_LIMITED = "RATE_LIMITED", "Rate limited"
    COOLDOWN = "COOLDOWN", "Cooldown"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED", "Manual review required"


PENDING_REQUEST_STATES = (
    AccessRequestState.REQUESTED,
    AccessRequestState.PENDING_VALIDATION,
    AccessRequestState.PENDING_IAS_APPROVAL,
)


class OrganizationUnit(models.Model):
    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "code")
        indexes = [models.Index(fields=("is_active", "name"), name="ia_org_active_name_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class AccessGrant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access_grants")
    organization = models.ForeignKey(OrganizationUnit, on_delete=models.PROTECT, related_name="access_grants")
    role = models.CharField(max_length=20, choices=AccessRole.choices)
    state = models.CharField(max_length=20, choices=AccessGrantState.choices, default=AccessGrantState.ACTIVE)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_access_grants")
    status_reason = models.CharField(max_length=1000, blank=True)
    status_changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="changed_access_grants")
    status_changed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "organization", "role"), name="ia_unique_user_org_role"),
            models.CheckConstraint(condition=Q(role__in=AccessRole.values), name="ia_grant_role_valid"),
            models.CheckConstraint(condition=Q(state__in=AccessGrantState.values), name="ia_grant_state_valid"),
            models.CheckConstraint(condition=Q(expires_at__isnull=True) | Q(expires_at__gt=models.F("starts_at")), name="ia_grant_expiry_after_start"),
        ]
        indexes = [
            models.Index(fields=("user", "state", "role"), name="ia_grant_user_state_idx"),
            models.Index(fields=("organization", "state", "role"), name="ia_grant_org_state_idx"),
        ]


class IASAuthorityScope(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ias_authority_scopes")
    organization = models.ForeignKey(OrganizationUnit, on_delete=models.PROTECT, related_name="ias_authorities")
    is_active = models.BooleanField(default=True)
    can_delegate = models.BooleanField(default=False)
    valid_until = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("user", "organization"), name="ia_unique_authority_scope")]
        indexes = [models.Index(fields=("user", "is_active", "valid_until"), name="ia_scope_user_active_idx")]


class AccessRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(max_length=120)
    official_email = models.EmailField(max_length=254)
    normalized_email = models.CharField(max_length=254)
    employee_identifier = models.CharField(max_length=64)
    normalized_identity = models.CharField(max_length=64)
    organization = models.ForeignKey(OrganizationUnit, on_delete=models.PROTECT, related_name="access_requests")
    designation = models.CharField(max_length=120, blank=True)
    reason = models.CharField(max_length=1000)
    state = models.CharField(max_length=30, choices=AccessRequestState.choices, default=AccessRequestState.REQUESTED)
    validation_outcome = models.CharField(max_length=30, choices=ValidationOutcome.choices)
    risk_flags = models.JSONField(default=list, blank=True)
    source_fingerprint = models.CharField(max_length=64)
    status_token_digest = models.CharField(max_length=64)
    requester_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="submitted_access_requests")
    version = models.PositiveIntegerField(default=1)
    expires_at = models.DateTimeField()
    decision_reason = models.CharField(max_length=1000, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="decided_access_requests")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(state__in=AccessRequestState.values), name="ia_request_state_valid"),
            models.CheckConstraint(condition=Q(validation_outcome__in=ValidationOutcome.values), name="ia_request_outcome_valid"),
            models.UniqueConstraint(fields=("normalized_email",), condition=Q(state__in=PENDING_REQUEST_STATES), name="ia_unique_pending_email"),
            models.UniqueConstraint(fields=("normalized_identity",), condition=~Q(normalized_identity="") & Q(state__in=PENDING_REQUEST_STATES), name="ia_unique_pending_identity"),
        ]
        indexes = [
            models.Index(fields=("state", "organization", "created_at"), name="ia_request_queue_idx"),
            models.Index(fields=("normalized_email", "state"), name="ia_request_email_idx"),
            models.Index(fields=("normalized_identity", "state"), name="ia_request_identity_idx"),
            models.Index(fields=("state", "expires_at"), name="ia_request_expiry_idx"),
        ]


class AccessRequestTransition(models.Model):
    request = models.ForeignKey(AccessRequest, on_delete=models.CASCADE, related_name="transitions")
    old_state = models.CharField(max_length=30, blank=True)
    new_state = models.CharField(max_length=30, choices=AccessRequestState.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="access_request_transitions")
    reason = models.CharField(max_length=1000, blank=True)
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [models.UniqueConstraint(fields=("request", "version"), name="ia_unique_request_version")]
        indexes = [models.Index(fields=("request", "created_at"), name="ia_transition_request_idx")]


class ActivationToken(models.Model):
    PURPOSE_ACCOUNT_ACTIVATION = "ACCOUNT_ACTIVATION"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="activation_tokens")
    access_request = models.ForeignKey(AccessRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="activation_tokens")
    token_digest = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=32, default=PURPOSE_ACCOUNT_ACTIVATION)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_activation_tokens")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("user", "purpose", "used_at", "expires_at"), name="ia_token_user_state_idx")]


class RateLimitBucket(models.Model):
    namespace = models.CharField(max_length=64)
    key_hash = models.CharField(max_length=64)
    window_started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("namespace", "key_hash"), name="ia_unique_throttle_bucket")]
        indexes = [
            models.Index(fields=("expires_at",), name="ia_throttle_expiry_idx"),
            models.Index(fields=("blocked_until",), name="ia_throttle_block_idx"),
        ]
