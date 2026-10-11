from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class WorkItemType(models.TextChoices):
    MISSING_DOCUMENT = "MISSING_DOCUMENT", "Missing document"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH", "Ambiguous match"
    IDENTITY_INCOMPLETE = "IDENTITY_INCOMPLETE", "Identity incomplete"
    PARSING_REVIEW = "PARSING_REVIEW", "Parsing review"
    DOCUMENT_EXISTS_BUT_UNRESOLVED = "DOCUMENT_EXISTS_BUT_UNRESOLVED", "Document exists but unresolved"


class WorkItemStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLAIMED = "CLAIMED", "Claimed"
    DOCUMENT_SUBMITTED = "DOCUMENT_SUBMITTED", "Document submitted"
    AWAITING_RESOLUTION = "AWAITING_RESOLUTION", "Awaiting resolution"
    RESOLVED_AUTO = "RESOLVED_AUTO", "Resolved automatically"
    RESOLVED_HUMAN = "RESOLVED_HUMAN", "Resolved by governed review"
    BLOCKED = "BLOCKED", "Blocked"
    CLOSED_INVALID = "CLOSED_INVALID", "Closed as invalid"


class WorkItemPriority(models.TextChoices):
    HIGH = "HIGH", "High"
    NORMAL = "NORMAL", "Normal"
    LOW = "LOW", "Low"


class MatchStatus(models.TextChoices):
    EXACT = "EXACT", "Exact deterministic match"
    STRONG = "STRONG", "Strong deterministic match"
    AMBIGUOUS = "AMBIGUOUS", "Ambiguous"
    NONE = "NONE", "No match"


class ProposalState(models.TextChoices):
    PENDING = "PENDING", "Pending IAS decision"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


ACTIVE_WORK_ITEM_STATUSES = (
    WorkItemStatus.OPEN,
    WorkItemStatus.CLAIMED,
    WorkItemStatus.DOCUMENT_SUBMITTED,
    WorkItemStatus.AWAITING_RESOLUTION,
    WorkItemStatus.BLOCKED,
)


class UnresolvedReferenceWorkItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_fingerprint = models.CharField(max_length=64, unique=True)
    organization = models.ForeignKey(
        "identity_access.OrganizationUnit",
        on_delete=models.PROTECT,
        related_name="unresolved_reference_work_items",
    )
    display_identifier = models.CharField(max_length=500, blank=True)
    normalized_target_identity = models.CharField(max_length=500, blank=True)
    reference_kind = models.CharField(max_length=48)
    target_date = models.DateField(null=True, blank=True)
    target_department = models.CharField(max_length=255, blank=True)
    work_type = models.CharField(max_length=48, choices=WorkItemType.choices)
    status = models.CharField(max_length=32, choices=WorkItemStatus.choices, default=WorkItemStatus.OPEN)
    priority = models.CharField(max_length=12, choices=WorkItemPriority.choices, default=WorkItemPriority.NORMAL)
    mention_count = models.PositiveIntegerField(default=0)
    distinct_source_document_count = models.PositiveIntegerField(default=0)
    local_match_status = models.CharField(max_length=16, choices=MatchStatus.choices, default=MatchStatus.NONE)
    local_catalog_id = models.PositiveBigIntegerField(null=True, blank=True)
    local_candidate_ids = models.JSONField(default=list, blank=True)
    corpus_match_status = models.CharField(max_length=16, choices=MatchStatus.choices, default=MatchStatus.NONE)
    corpus_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    corpus_candidate_ids = models.JSONField(default=list, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_unresolved_reference_work_items",
    )
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claimed_unresolved_reference_work_items",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    resolved_document_id = models.PositiveBigIntegerField(null=True, blank=True)
    resolution_method = models.CharField(max_length=128, blank=True)
    resolution_reason = models.CharField(max_length=1000, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    version = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-priority", "-mention_count", "created_at")
        constraints = [
            models.CheckConstraint(condition=Q(status__in=WorkItemStatus.values), name="kgui_work_status_valid"),
            models.CheckConstraint(condition=Q(work_type__in=WorkItemType.values), name="kgui_work_type_valid"),
            models.CheckConstraint(condition=Q(priority__in=WorkItemPriority.values), name="kgui_work_priority_valid"),
            models.CheckConstraint(condition=Q(local_match_status__in=MatchStatus.values), name="kgui_local_match_valid"),
            models.CheckConstraint(condition=Q(corpus_match_status__in=MatchStatus.values), name="kgui_corpus_match_valid"),
            models.CheckConstraint(
                condition=(Q(claimed_by__isnull=True, claimed_at__isnull=True, claim_expires_at__isnull=True)
                           | Q(claimed_by__isnull=False, claimed_at__isnull=False, claim_expires_at__isnull=False)),
                name="kgui_claim_fields_together",
            ),
        ]
        indexes = [
            models.Index(fields=("organization", "status", "priority"), name="kgui_work_scope_state_idx"),
            models.Index(fields=("status", "claim_expires_at"), name="kgui_work_claim_exp_idx"),
            models.Index(fields=("reference_kind", "target_date"), name="kgui_work_identity_idx"),
            models.Index(fields=("assigned_to", "status"), name="kgui_work_assignee_idx"),
        ]


class UnresolvedReferenceCandidateLink(models.Model):
    work_item = models.ForeignKey(
        UnresolvedReferenceWorkItem,
        on_delete=models.CASCADE,
        related_name="candidate_links",
    )
    candidate_id = models.PositiveBigIntegerField(unique=True)
    source_document_id = models.PositiveBigIntegerField()
    source_page = models.PositiveIntegerField()
    candidate_fingerprint = models.CharField(max_length=64)
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("candidate_id",)
        indexes = [
            models.Index(fields=("work_item", "source_document_id"), name="kgui_link_work_source_idx"),
        ]


class WorkItemSubmission(models.Model):
    work_item = models.ForeignKey(
        UnresolvedReferenceWorkItem,
        on_delete=models.PROTECT,
        related_name="submission_links",
    )
    submission = models.OneToOneField(
        "document_intake.DocumentSubmission",
        on_delete=models.PROTECT,
        related_name="unresolved_work_item_link",
    )
    local_catalog_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("work_item", "-created_at"), name="kgui_submission_work_idx")]


class ReferenceResolutionProposal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        UnresolvedReferenceWorkItem,
        on_delete=models.PROTECT,
        related_name="resolution_proposals",
    )
    target_document_id = models.PositiveBigIntegerField()
    relation_type = models.CharField(max_length=32, default="REFERENCES")
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reference_resolution_proposals",
    )
    reason = models.CharField(max_length=1000)
    evidence_snapshot = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=ProposalState.choices, default=ProposalState.PENDING)
    approval_case = models.OneToOneField(
        "governance.GovernanceApprovalCase",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reference_resolution_proposal",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="decided_reference_resolution_proposals",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=1000, blank=True)
    relation_ids = models.JSONField(default=list, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(state__in=ProposalState.values), name="kgui_proposal_state_valid"),
            models.UniqueConstraint(
                fields=("work_item",),
                condition=Q(state=ProposalState.PENDING),
                name="kgui_one_pending_proposal",
            ),
        ]
        indexes = [models.Index(fields=("work_item", "state"), name="kgui_proposal_work_idx")]


class WorkItemActivity(models.Model):
    work_item = models.ForeignKey(
        UnresolvedReferenceWorkItem,
        on_delete=models.PROTECT,
        related_name="activities",
    )
    action = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    summary = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("work_item", "-created_at"), name="kgui_activity_work_idx")]
