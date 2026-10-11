from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import connection, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.document_intake.models import SubmissionOrigin, SubmissionState
from apps.document_intake.services import DocumentIntakeError, quarantine_path, submit_document
from apps.governance.audit import record_governance_event
from apps.governance.constants import AuditAction, AuditCategory, AuditOutcome
from apps.governance.services import create_approval_case
from apps.identity_access.models import AccessGrant, AccessGrantState, AccessRole
from apps.identity_access.permissions import (
    authorized_organization_ids,
    can_approve_review,
    can_view_unresolved_reference,
    effective_grants,
    primary_access_role,
)
from knowledge_engine.documents.sources import LOCAL_ORIGINAL, resolve_document_path
from knowledge_engine.graph.normalization import normalize_document_number

from .models import (
    ACTIVE_WORK_ITEM_STATUSES,
    MatchStatus,
    ProposalState,
    ReferenceResolutionProposal,
    UnresolvedReferenceWorkItem,
    WorkItemActivity,
    WorkItemStatus,
    WorkItemSubmission,
)


WORKFLOW = "KG_REFERENCE_RESOLUTION"
SUBJECT_TYPE = "knowledge_graph_ui.ReferenceResolutionProposal"
ACTIVE_SUBMISSION_STATES = frozenset({
    SubmissionState.PENDING_IAS_APPROVAL,
    SubmissionState.QUEUED_FOR_INGESTION,
    SubmissionState.INGESTING,
})


class WorkbenchError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


def accessible_organization_ids(user) -> set[int]:
    officer = set(effective_grants(user, role=AccessRole.OFFICER).values_list("organization_id", flat=True))
    return officer | authorized_organization_ids(user)


def visible_work_items(user) -> QuerySet[UnresolvedReferenceWorkItem]:
    return UnresolvedReferenceWorkItem.objects.filter(organization_id__in=accessible_organization_ids(user))


def get_visible_work_item(user, item_id, *, lock: bool = False) -> UnresolvedReferenceWorkItem:
    queryset = visible_work_items(user).select_related("organization", "assigned_to", "claimed_by")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.get(pk=item_id)
    except (UnresolvedReferenceWorkItem.DoesNotExist, ValueError) as exc:
        raise WorkbenchError("WORK_ITEM_NOT_FOUND", "Unresolved-reference work item not found.") from exc


def _assert_version(item: UnresolvedReferenceWorkItem, expected_version: int) -> None:
    if item.version != expected_version:
        raise WorkbenchError("STALE_WORK_ITEM", "The work item changed; refresh before trying again.")


def _record_activity(item, action: str, summary: str, *, actor=None, metadata=None) -> WorkItemActivity:
    return WorkItemActivity.objects.create(
        work_item=item,
        action=action,
        actor=actor,
        summary=summary[:500],
        metadata=metadata or {},
    )


def _audit(item, action: str, reason_code: str, *, actor=None, reason: str = "", previous_state: str = "", metadata=None) -> None:
    record_governance_event(
        category=AuditCategory.APPROVAL,
        action=action,
        outcome=AuditOutcome.SUCCESS,
        actor=actor,
        actor_role=primary_access_role(actor) if actor is not None else "",
        organization_id=item.organization_id,
        resource_type="knowledge_graph_ui.UnresolvedReferenceWorkItem",
        resource_id=str(item.pk),
        resource_version=item.version,
        correlation_id=item.correlation_id,
        reason_code=reason_code,
        reason=reason,
        previous_state=previous_state,
        new_state=item.status,
        metadata=metadata or {},
    )


def _expire_claim_if_needed(item: UnresolvedReferenceWorkItem, now=None) -> bool:
    now = now or timezone.now()
    if item.claimed_by_id and item.claim_expires_at and item.claim_expires_at <= now:
        item.claimed_by = None
        item.claimed_at = None
        item.claim_expires_at = None
        if item.status == WorkItemStatus.CLAIMED:
            item.status = WorkItemStatus.OPEN
        return True
    return False


def claim_work_item(*, actor, item_id, expected_version: int) -> UnresolvedReferenceWorkItem:
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if item.status not in ACTIVE_WORK_ITEM_STATUSES:
            raise WorkbenchError("WORK_ITEM_NOT_ACTIVE", "This work item is no longer active.")
        now = timezone.now()
        _expire_claim_if_needed(item, now)
        if item.assigned_to_id and item.assigned_to_id != actor.pk and not can_approve_review(actor, item):
            raise WorkbenchError("ASSIGNED_TO_ANOTHER_OFFICER", "This work item is assigned to another officer.")
        if item.claimed_by_id and item.claimed_by_id != actor.pk:
            raise WorkbenchError("ALREADY_CLAIMED", "Another officer currently holds this work item.")
        previous = item.status
        item.claimed_by = actor
        item.claimed_at = now
        item.claim_expires_at = now + timedelta(minutes=settings.UNRESOLVED_CLAIM_MINUTES)
        item.status = WorkItemStatus.CLAIMED
        item.version += 1
        item.save(update_fields=("claimed_by", "claimed_at", "claim_expires_at", "status", "version", "updated_at"))
        _record_activity(item, AuditAction.UNRESOLVED_WORK_ITEM_CLAIMED, "Work item claimed.", actor=actor)
        _audit(
            item,
            AuditAction.UNRESOLVED_WORK_ITEM_CLAIMED,
            "WORK_ITEM_CLAIMED",
            actor=actor,
            previous_state=previous,
            metadata={"claim_expires_at": item.claim_expires_at.isoformat()},
        )
        return item


def release_work_item(*, actor, item_id, expected_version: int) -> UnresolvedReferenceWorkItem:
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if item.claimed_by_id is None:
            raise WorkbenchError("NOT_CLAIMED", "This work item does not have an active claim.")
        if item.claimed_by_id != actor.pk and not can_approve_review(actor, item):
            raise WorkbenchError("NOT_CLAIM_OWNER", "Only the claim owner or an in-scope IAS officer may release this claim.")
        previous = item.status
        item.claimed_by = None
        item.claimed_at = None
        item.claim_expires_at = None
        if item.status == WorkItemStatus.CLAIMED:
            item.status = WorkItemStatus.OPEN
        item.version += 1
        item.save(update_fields=("claimed_by", "claimed_at", "claim_expires_at", "status", "version", "updated_at"))
        _record_activity(item, AuditAction.UNRESOLVED_WORK_ITEM_RELEASED, "Work item claim released.", actor=actor)
        _audit(item, AuditAction.UNRESOLVED_WORK_ITEM_RELEASED, "WORK_ITEM_RELEASED", actor=actor, previous_state=previous)
        return item


def assign_work_item(*, actor, item_id, assignee_id: int, expected_version: int) -> UnresolvedReferenceWorkItem:
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if not can_approve_review(actor, item):
            raise WorkbenchError("IAS_SCOPE_REQUIRED", "In-scope IAS authority is required to assign work.")
        if item.status not in ACTIVE_WORK_ITEM_STATUSES:
            raise WorkbenchError("WORK_ITEM_NOT_ACTIVE", "This work item is no longer active.")
        grant = AccessGrant.objects.filter(
            user_id=assignee_id,
            organization_id=item.organization_id,
            role=AccessRole.OFFICER,
            state=AccessGrantState.ACTIVE,
        ).select_related("user").first()
        if grant is None or not grant.user.is_active:
            raise WorkbenchError("INVALID_ASSIGNEE", "The selected officer does not have an active grant in this scope.")
        item.assigned_to_id = assignee_id
        item.version += 1
        item.save(update_fields=("assigned_to", "version", "updated_at"))
        _record_activity(item, AuditAction.UNRESOLVED_WORK_ITEM_ASSIGNED, f"Assigned to {grant.user.get_username()}.", actor=actor)
        _audit(
            item,
            AuditAction.UNRESOLVED_WORK_ITEM_ASSIGNED,
            "WORK_ITEM_ASSIGNED",
            actor=actor,
            metadata={"assignee_id": assignee_id},
        )
        return item


def _can_act_on_item(actor, item: UnresolvedReferenceWorkItem) -> bool:
    if can_approve_review(actor, item):
        return True
    now = timezone.now()
    return bool(
        item.assigned_to_id == actor.pk
        or (item.claimed_by_id == actor.pk and item.claim_expires_at and item.claim_expires_at > now)
    )


def _ensure_no_active_submission(item: UnresolvedReferenceWorkItem) -> None:
    if WorkItemSubmission.objects.filter(work_item=item, submission__state__in=ACTIVE_SUBMISSION_STATES).exists():
        raise WorkbenchError("ACTIVE_SUBMISSION_EXISTS", "This work item already has an active document submission.")


def _link_submission(*, item, submission, actor, local_catalog_id=None) -> WorkItemSubmission:
    link = WorkItemSubmission.objects.create(
        work_item=item,
        submission=submission,
        local_catalog_id=local_catalog_id,
        created_by=actor,
    )
    previous = item.status
    item.status = (
        WorkItemStatus.DOCUMENT_SUBMITTED
        if submission.state != SubmissionState.VALIDATION_FAILED
        else WorkItemStatus.BLOCKED
    )
    item.version += 1
    item.save(update_fields=("status", "version", "updated_at"))
    _record_activity(
        item,
        AuditAction.UNRESOLVED_DOCUMENT_SUBMITTED,
        f"Linked secure document submission {submission.pk}.",
        actor=actor,
        metadata={"submission_id": str(submission.pk)},
    )
    metadata: dict[str, Any] = {"submission_id": str(submission.pk)}
    if local_catalog_id is not None:
        metadata["local_catalog_id"] = int(local_catalog_id)
    _audit(
        item,
        AuditAction.UNRESOLVED_DOCUMENT_SUBMITTED,
        "UNRESOLVED_DOCUMENT_SUBMITTED",
        actor=actor,
        previous_state=previous,
        metadata=metadata,
    )
    return link


def submit_work_item_upload(
    *, actor, item_id, expected_version: int, upload, claimed_identifier: str,
    claimed_source_url: str, submission_reason: str, source_context: str = "",
    ias_source_attested: bool = False,
):
    submission = None
    try:
        with transaction.atomic():
            item = get_visible_work_item(actor, item_id, lock=True)
            _assert_version(item, expected_version)
            if not _can_act_on_item(actor, item):
                raise WorkbenchError("CLAIM_REQUIRED", "Claim or assignment is required before submitting a document.")
            _ensure_no_active_submission(item)
            submission = submit_document(
                actor=actor,
                upload=upload,
                organization=item.organization,
                claimed_identifier=claimed_identifier or item.display_identifier,
                claimed_source_url=claimed_source_url,
                submission_reason=submission_reason,
                source_context=source_context,
                ias_source_attested=ias_source_attested,
                origin=SubmissionOrigin.UNRESOLVED_REFERENCE,
                origin_reference=str(item.pk),
            )
            _link_submission(item=item, submission=submission, actor=actor)
            return submission
    except Exception:
        if submission is not None and not type(submission).objects.filter(pk=submission.pk).exists():
            path = quarantine_path(submission)
            path.unlink(missing_ok=True)
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        raise


def local_catalog_record(catalog_id: int) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,relative_path,filename,sha256,page_count,document_kind,
                      normalized_document_number,normalized_date,department_folder,
                      identity_quality,identity_parse_status,ingested_document_id
                   FROM local_document_catalog WHERE id=%s""",
            [catalog_id],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row, strict=True))


def submit_local_catalog_document(*, actor, item_id, expected_version: int, catalog_id: int, submission_reason: str, ias_source_attested: bool = False):
    submission = None
    try:
        with transaction.atomic():
            item = get_visible_work_item(actor, item_id, lock=True)
            _assert_version(item, expected_version)
            if not _can_act_on_item(actor, item):
                raise WorkbenchError("CLAIM_REQUIRED", "Claim or assignment is required before submitting a document.")
            _ensure_no_active_submission(item)
            allowed_ids = {int(value) for value in item.local_candidate_ids}
            if item.local_match_status not in {MatchStatus.EXACT, MatchStatus.STRONG} or catalog_id not in allowed_ids:
                raise WorkbenchError("LOCAL_MATCH_NOT_ELIGIBLE", "Only the work item's deterministic local match may be submitted.")
            record = local_catalog_record(catalog_id)
            if record is None:
                raise WorkbenchError("LOCAL_DOCUMENT_NOT_FOUND", "The selected local catalog record no longer exists.")
            path = resolve_document_path(LOCAL_ORIGINAL, str(record["relative_path"]))
            if not path.is_file():
                raise WorkbenchError("LOCAL_DOCUMENT_NOT_FOUND", "The selected local PDF is not available.")
            with path.open("rb") as handle:
                upload = File(handle, name=str(record["filename"]))
                upload.content_type = "application/pdf"
                submission = submit_document(
                    actor=actor,
                    upload=upload,
                    organization=item.organization,
                    claimed_identifier=item.display_identifier,
                    claimed_source_url="",
                    submission_reason=submission_reason,
                    source_context="Selected from the verified local document catalog.",
                    ias_source_attested=ias_source_attested,
                    origin=SubmissionOrigin.UNRESOLVED_REFERENCE,
                    origin_reference=str(item.pk),
                )
            _link_submission(item=item, submission=submission, actor=actor, local_catalog_id=catalog_id)
            return submission
    except Exception:
        if submission is not None and not type(submission).objects.filter(pk=submission.pk).exists():
            path = quarantine_path(submission)
            path.unlink(missing_ok=True)
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        raise


def evidence_for_work_item(item: UnresolvedReferenceWorkItem, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    candidate_ids = list(item.candidate_links.values_list("candidate_id", flat=True))
    total = len(candidate_ids)
    if not candidate_ids:
        return {"items": [], "page": page, "pages": 0, "total": 0}
    offset = (page - 1) * page_size
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id,c.source_document_id,c.source_page,c.reference_kind,c.raw_evidence_text,
                   c.raw_document_number,c.normalized_document_number,c.raw_reference_date,
                   c.normalized_reference_date,c.raw_department,c.normalized_department,
                   c.parser_status,c.resolution_status,c.validation_status,c.fingerprint,
                   s.file_name AS source_file,s.department AS source_department
              FROM kg_reference_candidates c
              JOIN documents s ON s.id=c.source_document_id
             WHERE c.id=ANY(%s)
             ORDER BY c.source_document_id,c.source_page,c.id
             LIMIT %s OFFSET %s
            """,
            [candidate_ids, page_size, offset],
        )
        columns = [column[0] for column in cursor.description]
        items = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return {"items": items, "page": page, "pages": (total + page_size - 1) // page_size, "total": total}


def document_summaries(document_ids: list[int]) -> list[dict[str, Any]]:
    ids = sorted({int(value) for value in document_ids})[:25]
    if not ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,file_name,department,document_kind,gr_number,gr_date,subject
                   FROM documents WHERE id=ANY(%s) ORDER BY id""",
            [ids],
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _proposal_digest(proposal: ReferenceResolutionProposal) -> str:
    payload = {
        "proposal_id": str(proposal.pk),
        "work_item_id": str(proposal.work_item_id),
        "target_document_id": proposal.target_document_id,
        "relation_type": proposal.relation_type,
        "evidence_snapshot": proposal.evidence_snapshot,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_target(item: UnresolvedReferenceWorkItem, target_document_id: int) -> dict[str, Any]:
    documents = document_summaries([target_document_id])
    if not documents:
        raise WorkbenchError("TARGET_NOT_FOUND", "The proposed target document does not exist in the corpus.")
    target = documents[0]
    allowed = {int(value) for value in item.corpus_candidate_ids}
    target_identity = normalize_document_number(str(target.get("gr_number") or ""))
    if target_document_id not in allowed and target_identity != item.normalized_target_identity:
        raise WorkbenchError("TARGET_IDENTITY_MISMATCH", "The proposed document does not match the work item's normalized identity evidence.")
    if target_document_id in set(item.candidate_links.values_list("source_document_id", flat=True)):
        raise WorkbenchError("SELF_REFERENCE_REJECTED", "A source document cannot resolve its own reference target.")
    return target


def propose_resolution(*, actor, item_id, expected_version: int, target_document_id: int, reason: str) -> ReferenceResolutionProposal:
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > 1000:
        raise WorkbenchError("INVALID_REASON", "A bounded resolution reason is required.")
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if item.status not in ACTIVE_WORK_ITEM_STATUSES:
            raise WorkbenchError("WORK_ITEM_NOT_ACTIVE", "This work item is no longer active.")
        if not _can_act_on_item(actor, item):
            raise WorkbenchError("CLAIM_REQUIRED", "Claim or assignment is required before proposing a resolution.")
        if ReferenceResolutionProposal.objects.filter(work_item=item, state=ProposalState.PENDING).exists():
            raise WorkbenchError("PENDING_PROPOSAL_EXISTS", "This work item already has a pending resolution proposal.")
        target = _validate_target(item, target_document_id)
        candidate_ids = list(item.candidate_links.values_list("candidate_id", flat=True).order_by("candidate_id"))
        proposal = ReferenceResolutionProposal.objects.create(
            work_item=item,
            target_document_id=target_document_id,
            proposed_by=actor,
            reason=clean_reason,
            evidence_snapshot={
                "candidate_ids": candidate_ids[:20],
                "candidate_count": len(candidate_ids),
                "target_file": str(target.get("file_name") or "")[:255],
                "target_identity": str(target.get("gr_number") or "")[:255],
            },
        )
        case = create_approval_case(
            workflow_type=WORKFLOW,
            organization=item.organization,
            submitted_by=actor,
            subject_type=SUBJECT_TYPE,
            subject_id=str(proposal.pk),
            subject_version=proposal.version,
            subject_digest=_proposal_digest(proposal),
            correlation_id=proposal.correlation_id,
        )
        proposal.approval_case = case
        proposal.save(update_fields=("approval_case", "updated_at"))
        previous = item.status
        item.status = WorkItemStatus.AWAITING_RESOLUTION
        item.version += 1
        item.save(update_fields=("status", "version", "updated_at"))
        _record_activity(item, AuditAction.REFERENCE_RESOLUTION_PROPOSED, f"Proposed corpus document {target_document_id}.", actor=actor)
        _audit(
            item,
            AuditAction.REFERENCE_RESOLUTION_PROPOSED,
            "REFERENCE_RESOLUTION_PROPOSED",
            actor=actor,
            reason=clean_reason,
            previous_state=previous,
            metadata={
                "candidate_count": len(candidate_ids),
                "target_document_id": target_document_id,
                "proposal_id": str(proposal.pk),
            },
        )
        return proposal


def close_invalid(*, actor, item_id, expected_version: int, reason: str) -> UnresolvedReferenceWorkItem:
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > 1000:
        raise WorkbenchError("INVALID_REASON", "A bounded closure reason is required.")
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if not can_approve_review(actor, item):
            raise WorkbenchError("IAS_SCOPE_REQUIRED", "In-scope IAS authority is required to close an invalid target.")
        if ReferenceResolutionProposal.objects.filter(work_item=item, state=ProposalState.PENDING).exists():
            raise WorkbenchError("PENDING_PROPOSAL_EXISTS", "Decide the pending resolution proposal before closing this work item.")
        previous = item.status
        item.status = WorkItemStatus.CLOSED_INVALID
        item.resolution_method = "IAS_INVALID_TARGET_CLOSURE"
        item.resolution_reason = clean_reason
        item.claimed_by = None
        item.claimed_at = None
        item.claim_expires_at = None
        item.version += 1
        item.save()
        _record_activity(item, AuditAction.WORK_ITEM_CLOSED_INVALID, "Closed as invalid by IAS authority.", actor=actor)
        _audit(item, AuditAction.WORK_ITEM_CLOSED_INVALID, "WORK_ITEM_CLOSED_INVALID", actor=actor, reason=clean_reason, previous_state=previous)
        return item


def refresh_work_item_from_graph(item_id, *, actor=None) -> UnresolvedReferenceWorkItem:
    with transaction.atomic():
        item = UnresolvedReferenceWorkItem.objects.select_for_update().get(pk=item_id)
        candidate_ids = list(item.candidate_links.values_list("candidate_id", flat=True))
        if not candidate_ids:
            return item
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,resolution_status,resolved_target_document_id,validation_status
                       FROM kg_reference_candidates WHERE id=ANY(%s) ORDER BY id FOR UPDATE""",
                [candidate_ids],
            )
            rows = cursor.fetchall()
        resolved = [row for row in rows if row[1] in {"RESOLVED_EXACT", "RESOLVED_STRONG"} and row[2] is not None]
        targets = {int(row[2]) for row in resolved}
        if len(resolved) != len(rows) or len(targets) != 1:
            if item.status == WorkItemStatus.DOCUMENT_SUBMITTED:
                item.status = WorkItemStatus.AWAITING_RESOLUTION
                item.version += 1
                item.save(update_fields=("status", "version", "updated_at"))
            return item
        human = any(row[3] == "HUMAN_CONFIRMED" for row in resolved)
        desired = WorkItemStatus.RESOLVED_HUMAN if human else WorkItemStatus.RESOLVED_AUTO
        if item.status != desired or item.resolved_document_id != next(iter(targets)):
            previous = item.status
            item.status = desired
            item.resolved_document_id = next(iter(targets))
            item.resolution_method = "HUMAN_GOVERNED_MATCH" if human else "TARGETED_KG_AUTO_RESOLUTION"
            item.claimed_by = None
            item.claimed_at = None
            item.claim_expires_at = None
            item.version += 1
            item.save()
            if not human:
                _record_activity(item, AuditAction.REFERENCE_RESOLVED_AUTO, "Resolved by deterministic targeted KG processing.", actor=actor)
                _audit(
                    item,
                    AuditAction.REFERENCE_RESOLVED_AUTO,
                    "REFERENCE_RESOLVED_AUTO",
                    actor=actor,
                    previous_state=previous,
                    metadata={"candidate_count": len(rows), "target_document_id": item.resolved_document_id, "resolution_method": item.resolution_method},
                )
        return item


def refresh_linked_work_item_for_submission(submission) -> UnresolvedReferenceWorkItem | None:
    try:
        link = WorkItemSubmission.objects.select_related("work_item").get(submission=submission)
    except WorkItemSubmission.DoesNotExist:
        return None
    return refresh_work_item_from_graph(link.work_item_id)


def resolve_work_item_automatically(*, actor, item_id, expected_version: int) -> UnresolvedReferenceWorkItem:
    with transaction.atomic():
        item = get_visible_work_item(actor, item_id, lock=True)
        _assert_version(item, expected_version)
        if not _can_act_on_item(actor, item):
            raise WorkbenchError("CLAIM_REQUIRED", "Claim or assignment is required before resolving a work item.")
        if item.corpus_match_status != MatchStatus.EXACT or item.corpus_document_id is None:
            raise WorkbenchError("AUTO_RESOLUTION_NOT_ELIGIBLE", "This target has no unique deterministic corpus match.")
        target_document_id = item.corpus_document_id
        _validate_target(item, target_document_id)
    from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline

    KnowledgeGraphPipeline().run_targeted(target_document_id)
    refreshed = refresh_work_item_from_graph(item_id, actor=actor)
    if refreshed.status != WorkItemStatus.RESOLVED_AUTO:
        raise WorkbenchError(
            "AUTO_RESOLUTION_GATE_FAILED",
            "The authoritative resolver did not approve every candidate; use governed human review instead.",
        )
    return refreshed


def confirm_human_resolution(*, proposal: ReferenceResolutionProposal, reviewer, reason: str) -> list[int]:
    item = UnresolvedReferenceWorkItem.objects.select_for_update().get(pk=proposal.work_item_id)
    candidate_ids = list(item.candidate_links.values_list("candidate_id", flat=True).order_by("candidate_id"))
    if not candidate_ids:
        raise WorkbenchError("NO_CANDIDATE_EVIDENCE", "The work item has no authoritative candidate evidence.")
    _validate_target(item, proposal.target_document_id)
    relation_ids: list[int] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,source_document_id,resolution_status,validation_status
                   FROM kg_reference_candidates WHERE id=ANY(%s) ORDER BY id FOR UPDATE""",
            [candidate_ids],
        )
        rows = cursor.fetchall()
        if len(rows) != len(candidate_ids):
            raise WorkbenchError("CANDIDATE_EVIDENCE_CHANGED", "Authoritative candidate evidence changed; refresh and review again.")
        for candidate_id, source_document_id, resolution_status, validation_status in rows:
            if resolution_status not in {"NOT_IN_CORPUS", "AMBIGUOUS", "UNPARSEABLE", "NEEDS_REVIEW"}:
                raise WorkbenchError("CANDIDATE_ALREADY_RESOLVED", "A candidate changed after this proposal was created.")
            cursor.execute(
                """INSERT INTO kg_relations(source_document_id,target_document_id,relation_type,validation_status,created_from_candidate_id)
                   VALUES (%s,%s,'REFERENCES','HUMAN_CONFIRMED',%s)
                   ON CONFLICT DO NOTHING RETURNING id""",
                [source_document_id, proposal.target_document_id, candidate_id],
            )
            relation = cursor.fetchone()
            if relation is None:
                cursor.execute(
                    """SELECT id,validation_status FROM kg_relations
                         WHERE source_document_id=%s AND target_document_id=%s AND relation_type='REFERENCES'
                         FOR UPDATE""",
                    [source_document_id, proposal.target_document_id],
                )
                relation_id, relation_validation = cursor.fetchone()
                if relation_validation != "HUMAN_CONFIRMED":
                    cursor.execute("UPDATE kg_relations SET validation_status='HUMAN_CONFIRMED',updated_at=now() WHERE id=%s", [relation_id])
            else:
                relation_id = relation[0]
            cursor.execute(
                "INSERT INTO kg_relation_evidence(relation_id,candidate_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                [relation_id, candidate_id],
            )
            cursor.execute(
                """UPDATE kg_reference_candidates SET
                           resolution_status='RESOLVED_STRONG',resolved_target_document_id=%s,
                           resolution_method=%s,resolution_evidence=%s,resolution_candidate_count=1,
                           confidence_category='HIGH',validation_status='HUMAN_CONFIRMED',
                           reviewer=%s,review_note=%s,reviewed_at=now(),updated_at=now()
                     WHERE id=%s""",
                [
                    proposal.target_document_id,
                    f"GOVERNED_PROPOSAL:{proposal.pk}",
                    f"IAS-approved work item {item.pk}; approval case {proposal.approval_case_id}",
                    reviewer.get_username(),
                    reason,
                    candidate_id,
                ],
            )
            cursor.execute(
                """INSERT INTO kg_reviews(candidate_id,previous_status,decision,reviewer,review_note)
                   VALUES (%s,%s,'HUMAN_CONFIRMED',%s,%s)""",
                [candidate_id, validation_status, reviewer.get_username(), reason],
            )
            relation_ids.append(int(relation_id))
    return sorted(set(relation_ids))
