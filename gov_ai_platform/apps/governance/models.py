from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .constants import (
    GENESIS_HASH,
    ApprovalCaseState,
    ApprovalDecisionType,
    AuditAction,
    AuditCategory,
    AuditOrigin,
    AuditOutcome,
)
from .exceptions import AuditValidationError


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditValidationError("Append-only records cannot be updated.")

    def delete(self):
        raise AuditValidationError("Append-only records cannot be deleted.")


class GovernanceApprovalCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow_type = models.CharField(max_length=64)
    organization = models.ForeignKey(
        "identity_access.OrganizationUnit",
        on_delete=models.PROTECT,
        related_name="governance_approval_cases",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_governance_cases",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    subject_type = models.CharField(max_length=64)
    subject_id = models.CharField(max_length=128)
    subject_version = models.PositiveBigIntegerField()
    subject_digest = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=20, choices=ApprovalCaseState.choices, default=ApprovalCaseState.PENDING)
    version = models.PositiveIntegerField(default=1)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(state__in=ApprovalCaseState.values), name="gov_case_state_valid"),
        ]
        indexes = [
            models.Index(fields=("organization", "state", "created_at"), name="gov_case_scope_state_idx"),
            models.Index(fields=("workflow_type", "state"), name="gov_case_workflow_idx"),
            models.Index(fields=("subject_type", "subject_id"), name="gov_case_subject_idx"),
        ]


class GovernanceApprovalDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(GovernanceApprovalCase, on_delete=models.PROTECT, related_name="decisions")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="governance_decisions")
    actor_role_snapshot = models.CharField(max_length=32)
    organization_id_snapshot = models.PositiveBigIntegerField()
    decision = models.CharField(max_length=16, choices=ApprovalDecisionType.choices)
    reason = models.CharField(max_length=1000)
    subject_version_seen = models.PositiveBigIntegerField()
    case_version_seen = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(decision__in=ApprovalDecisionType.values), name="gov_decision_valid"),
            models.UniqueConstraint(fields=("case",), name="gov_case_one_final_decision"),
        ]
        indexes = [models.Index(fields=("organization_id_snapshot", "created_at"), name="gov_decision_scope_idx")]

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_governance_insert_allowed", False):
            raise AuditValidationError("Approval decisions are append-only and must use the governance service.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditValidationError("Approval decisions cannot be deleted.")


class GovernanceAuditChain(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    last_sequence = models.PositiveBigIntegerField(default=0)
    head_hash = models.CharField(max_length=64, default=GENESIS_HASH)
    updated_at = models.DateTimeField(auto_now=True)


class GovernanceAuditEvent(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.PositiveBigIntegerField(unique=True)
    schema_version = models.PositiveSmallIntegerField()
    occurred_at = models.DateTimeField()
    category = models.CharField(max_length=32, choices=AuditCategory.choices)
    action = models.CharField(max_length=64, choices=AuditAction.choices)
    outcome = models.CharField(max_length=16, choices=AuditOutcome.choices)
    actor_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor_role_snapshot = models.CharField(max_length=32, blank=True)
    organization_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    resource_type = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=128)
    resource_version = models.PositiveBigIntegerField(null=True, blank=True)
    correlation_id = models.UUIDField(db_index=True)
    reason_code = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=1000, blank=True)
    previous_state = models.CharField(max_length=64, blank=True)
    new_state = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    previous_hash = models.CharField(max_length=64)
    event_hash = models.CharField(max_length=64)
    origin = models.CharField(max_length=32, choices=AuditOrigin.choices, default=AuditOrigin.LIVE)
    source_identifier = models.CharField(max_length=128, null=True, blank=True, unique=True)
    source_occurred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-sequence",)
        constraints = [
            models.CheckConstraint(condition=Q(category__in=AuditCategory.values), name="gov_audit_category_valid"),
            models.CheckConstraint(condition=Q(action__in=AuditAction.values), name="gov_audit_action_valid"),
            models.CheckConstraint(condition=Q(outcome__in=AuditOutcome.values), name="gov_audit_outcome_valid"),
            models.CheckConstraint(condition=Q(origin__in=AuditOrigin.values), name="gov_audit_origin_valid"),
        ]
        indexes = [
            models.Index(fields=("organization_id_snapshot", "-created_at"), name="gov_audit_scope_time_idx"),
            models.Index(fields=("category", "action", "-created_at"), name="gov_audit_action_time_idx"),
            models.Index(fields=("actor_user_id", "-created_at"), name="gov_audit_actor_time_idx"),
            models.Index(fields=("resource_type", "resource_id"), name="gov_audit_resource_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_audit_insert_allowed", False):
            raise AuditValidationError("Audit events are append-only and must use the audit service.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditValidationError("Audit events cannot be deleted.")
