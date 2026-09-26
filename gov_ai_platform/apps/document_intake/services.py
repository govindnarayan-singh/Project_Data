from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path, PurePath

from django.conf import settings
from django.db import connection, transaction

from apps.governance.audit import record_governance_event
from apps.governance.constants import AuditAction, AuditCategory, AuditOutcome
from apps.governance.services import create_approval_case
from apps.identity_access.models import AccessRole
from apps.identity_access.permissions import can_approve_document, can_submit_document, primary_access_role

from .governance import SUBJECT_TYPE, WORKFLOW
from .models import (
    ACTIVE_DIGEST_STATES,
    DocumentSubmission,
    DuplicateStatus,
    MalwareStatus,
    SecurityStatus,
    SourceProvenance,
    SubmissionOrigin,
    SubmissionState,
    ValidationState,
)
from .validation import DocumentValidationError, validate_pdf


class DocumentIntakeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


def authorized_submission_organizations(user):
    from apps.identity_access.models import OrganizationUnit
    from apps.identity_access.permissions import authorized_organization_ids, effective_grants

    officer_ids = effective_grants(user, role=AccessRole.OFFICER).values_list("organization_id", flat=True)
    ids = set(officer_ids) | authorized_organization_ids(user)
    return OrganizationUnit.objects.filter(pk__in=ids, is_active=True).order_by("name")


def can_view_submission(user, submission: DocumentSubmission) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and (submission.submitted_by_id == user.pk or can_approve_document(user, submission.organization_id))
    )


def quarantine_path(submission_or_key) -> Path:
    key = submission_or_key.storage_key if hasattr(submission_or_key, "storage_key") else str(submission_or_key)
    root = Path(settings.DOCUMENT_QUARANTINE_ROOT).resolve()
    candidate = (root / key).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DocumentIntakeError("UNSAFE_STORAGE_KEY", "The internal storage reference is invalid.") from exc
    return candidate


def _write_upload(upload, submission_id: uuid.UUID) -> tuple[str, Path, str, int]:
    storage_key = f"{submission_id}/original.pdf"
    destination = quarantine_path(storage_key)
    destination.parent.mkdir(parents=True, exist_ok=False)
    temporary = destination.with_suffix(".uploading")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as handle:
            for chunk in upload.chunks():
                size += len(chunk)
                if size > settings.DOCUMENT_MAX_UPLOAD_BYTES:
                    raise DocumentIntakeError("FILE_TOO_LARGE", "The PDF exceeds the configured upload limit.")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        if destination.parent.exists() and not any(destination.parent.iterdir()):
            destination.parent.rmdir()
        raise
    return storage_key, destination, digest.hexdigest(), size


def _corpus_match(sha256: str, metadata: dict) -> tuple[int | None, bool]:
    if "documents" not in connection.introspection.table_names():
        return None, False
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM documents WHERE sha256=%s LIMIT 1", [sha256])
        row = cursor.fetchone()
        if row:
            return int(row[0]), False
        identifier = str(metadata.get("gr_number") or "").strip()
        if identifier:
            cursor.execute("SELECT id FROM documents WHERE lower(coalesce(gr_number,''''))=lower(%s) LIMIT 1", [identifier])
            row = cursor.fetchone()
            if row:
                return int(row[0]), True
    return None, False


def submit_document(*, actor, upload, organization, claimed_identifier: str, claimed_source_url: str,
                    source_provenance: str, submission_reason: str,
                    origin: str = SubmissionOrigin.DIRECT_UPLOAD, origin_reference: str = "") -> DocumentSubmission:
    if not can_submit_document(actor, organization):
        raise DocumentIntakeError("OUT_OF_SCOPE", "You are not authorized to submit for this organization.")
    if origin not in SubmissionOrigin.values:
        raise DocumentIntakeError("INVALID_ORIGIN", "The submission origin is invalid.")
    role = primary_access_role(actor)
    if role == AccessRole.OFFICER:
        source_provenance = SourceProvenance.OFFICER_PROVIDED_UNVERIFIED
    elif source_provenance not in {SourceProvenance.IAS_VERIFIED_COPY, SourceProvenance.UNKNOWN}:
        source_provenance = SourceProvenance.UNKNOWN
    raw_name = str(getattr(upload, "name", "upload.pdf"))[:1000]
    normalized_name = raw_name.replace("\\", "/")
    safe_name = PurePath(normalized_name).name[:255] or "upload.pdf"
    filename_flags = ["UNSAFE_ORIGINAL_FILENAME_NORMALIZED"] if safe_name != raw_name else []
    submission_id = uuid.uuid4()
    storage_key = ""
    path: Path | None = None
    try:
        storage_key, path, digest, byte_size = _write_upload(upload, submission_id)
        validation_error: DocumentValidationError | None = None
        try:
            validated = validate_pdf(
                path,
                original_filename=safe_name,
                declared_mime=str(getattr(upload, "content_type", "")),
                department=organization.name,
            )
        except DocumentValidationError as exc:
            validation_error = exc
            validated = None

        matched_pending = DocumentSubmission.objects.filter(sha256=digest, state__in=ACTIVE_DIGEST_STATES).first()
        corpus_document_id, identity_match = _corpus_match(digest, validated.detected_metadata if validated else {})
        duplicate_status = DuplicateStatus.NEW
        if matched_pending:
            duplicate_status = DuplicateStatus.EXACT_PENDING_DUPLICATE
        elif corpus_document_id and not identity_match:
            duplicate_status = DuplicateStatus.EXACT_CORPUS_DUPLICATE
        elif identity_match:
            duplicate_status = DuplicateStatus.POSSIBLE_IDENTITY_DUPLICATE

        failed = validation_error is not None or duplicate_status in {
            DuplicateStatus.EXACT_PENDING_DUPLICATE, DuplicateStatus.EXACT_CORPUS_DUPLICATE
        }
        failure_code = validation_error.code if validation_error else (duplicate_status if failed else "")
        failure_reason = validation_error.safe_message if validation_error else (
            "Identical bytes already exist in a pending submission or the corpus." if failed else ""
        )
        with transaction.atomic():
            submission = DocumentSubmission.objects.create(
                id=submission_id,
                organization=organization,
                submitted_by=actor,
                submitter_role_snapshot=role,
                original_filename=safe_name,
                storage_key=storage_key,
                sha256=digest,
                byte_size=byte_size,
                declared_mime=str(getattr(upload, "content_type", ""))[:128],
                state=SubmissionState.VALIDATION_FAILED if failed else SubmissionState.PENDING_IAS_APPROVAL,
                validation_state=ValidationState.FAILED if failed else ValidationState.PASSED,
                security_status=SecurityStatus.FAILED if validation_error else SecurityStatus.PASSED,
                malware_status=(validated.malware_status if validated else MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED),
                source_provenance=source_provenance,
                claimed_source_url=claimed_source_url,
                claimed_identifier=claimed_identifier,
                submission_reason=submission_reason,
                detected_metadata=(validated.detected_metadata if validated else {}),
                duplicate_status=duplicate_status,
                matched_submission=matched_pending,
                matched_document_id=corpus_document_id,
                page_count=(validated.page_count if validated else 0),
                extraction_status=(validated.extraction_status if validated else "NOT_EXTRACTED"),
                preview_text=(validated.preview_text if validated else ""),
                validation_findings=((validated.findings if validated else (validation_error.findings if validation_error else []))),
                risk_flags=filename_flags + (validated.risk_flags if validated else []),
                origin=origin,
                origin_reference=origin_reference[:128],
                safe_failure_code=str(failure_code),
                safe_failure_reason=failure_reason,
            )
            record_governance_event(
                category=AuditCategory.DOCUMENT,
                action=AuditAction.DOCUMENT_SUBMITTED,
                outcome=AuditOutcome.SUCCESS,
                actor=actor,
                actor_role=role,
                organization_id=organization.pk,
                resource_type=SUBJECT_TYPE,
                resource_id=str(submission.pk),
                resource_version=submission.version,
                correlation_id=submission.correlation_id,
                reason_code="DOCUMENT_SUBMITTED",
                new_state=submission.state,
                metadata={"byte_size": byte_size, "duplicate_status": duplicate_status, "source_provenance": source_provenance},
            )
            if failed:
                record_governance_event(
                    category=AuditCategory.SECURITY if validation_error else AuditCategory.DOCUMENT,
                    action=AuditAction.DOCUMENT_VALIDATION_FAILED,
                    outcome=AuditOutcome.FAILURE,
                    actor=actor,
                    actor_role=role,
                    organization_id=organization.pk,
                    resource_type=SUBJECT_TYPE,
                    resource_id=str(submission.pk),
                    resource_version=submission.version,
                    correlation_id=submission.correlation_id,
                    reason_code=str(failure_code),
                    reason=failure_reason,
                    new_state=submission.state,
                    metadata={"duplicate_status": duplicate_status, "failure_code": str(failure_code)},
                )
            else:
                case = create_approval_case(
                    workflow_type=WORKFLOW,
                    organization=organization,
                    submitted_by=actor,
                    subject_type=SUBJECT_TYPE,
                    subject_id=str(submission.pk),
                    subject_version=submission.version,
                    subject_digest=submission.sha256,
                    correlation_id=submission.correlation_id,
                )
                submission.approval_case = case
                submission.save(update_fields=("approval_case", "updated_at"))
                record_governance_event(
                    category=AuditCategory.DOCUMENT,
                    action=AuditAction.DOCUMENT_READY_FOR_APPROVAL,
                    outcome=AuditOutcome.SUCCESS,
                    actor=actor,
                    actor_role=role,
                    organization_id=organization.pk,
                    resource_type=SUBJECT_TYPE,
                    resource_id=str(submission.pk),
                    resource_version=submission.version,
                    correlation_id=submission.correlation_id,
                    reason_code="VALIDATION_PASSED",
                    new_state=submission.state,
                    metadata={"page_count": submission.page_count, "duplicate_status": duplicate_status},
                )
        return submission
    except Exception:
        if path is not None and path.exists() and not DocumentSubmission.objects.filter(pk=submission_id).exists():
            path.unlink(missing_ok=True)
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        raise
