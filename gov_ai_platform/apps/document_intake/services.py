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
    DocumentValidationAttempt,
    DuplicateStatus,
    MalwareStatus,
    SecurityStatus,
    SignatureStatus,
    SourceProvenance,
    SubmissionOrigin,
    SubmissionState,
    ValidationState,
)
from .validation import VALIDATOR_VERSION, DocumentValidationError, validate_pdf


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


def can_revalidate_submission(user, submission: DocumentSubmission) -> bool:
    if submission.state != SubmissionState.VALIDATION_FAILED:
        return False
    if submission.duplicate_status in {
        DuplicateStatus.EXACT_PENDING_DUPLICATE,
        DuplicateStatus.EXACT_CORPUS_DUPLICATE,
    }:
        return False
    if not can_submit_document(user, submission.organization_id):
        return False
    return submission.submitted_by_id == getattr(user, "pk", None) or can_approve_document(
        user, submission.organization_id
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
                    submission_reason: str, source_context: str = "", ias_source_attested: bool = False,
                    source_provenance: str | None = None,
                    origin: str = SubmissionOrigin.DIRECT_UPLOAD, origin_reference: str = "") -> DocumentSubmission:
    if not can_submit_document(actor, organization):
        raise DocumentIntakeError("OUT_OF_SCOPE", "You are not authorized to submit for this organization.")
    if origin not in SubmissionOrigin.values:
        raise DocumentIntakeError("INVALID_ORIGIN", "The submission origin is invalid.")
    role = primary_access_role(actor)
    if role == AccessRole.OFFICER:
        source_provenance = SourceProvenance.OFFICER_PROVIDED_UNVERIFIED
        ias_source_attested = False
    elif role == AccessRole.IAS_OFFICER and ias_source_attested:
        source_provenance = SourceProvenance.IAS_ATTESTED_OFFICIAL_COPY
    else:
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

        inspection = validation_error.inspection if validation_error else None

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
                malware_status=(validated.malware_status if validated else (inspection.malware_status if inspection else MalwareStatus.NOT_PERFORMED)),
                source_provenance=source_provenance,
                source_context=source_context.strip()[:1000],
                ias_source_attested=bool(ias_source_attested and role == AccessRole.IAS_OFFICER),
                claimed_source_url=claimed_source_url,
                claimed_identifier=claimed_identifier,
                submission_reason=submission_reason,
                detected_metadata=(validated.detected_metadata if validated else {}),
                duplicate_status=duplicate_status,
                matched_submission=matched_pending,
                matched_document_id=corpus_document_id,
                page_count=(validated.page_count if validated else (inspection.page_count if inspection else None)),
                extraction_status=(validated.extraction_status if validated else (inspection.extraction_status if inspection else "NOT_STARTED")),
                preview_text=(validated.preview_text if validated else ""),
                validation_findings=((validated.findings if validated else (validation_error.findings if validation_error else []))),
                risk_flags=filename_flags + (validated.risk_flags if validated else (inspection.risk_flags if inspection else [])),
                validator_version=VALIDATOR_VERSION,
                signature_status=(validated.signature_status if validated else (inspection.signature_status if inspection else SignatureStatus.NOT_CHECKED)),
                signature_summary=(validated.signature_summary if validated else (inspection.signature_summary if inspection else [])),
                external_uri_domains=(validated.external_uri_domains if validated else (inspection.external_uri_domains if inspection else [])),
                origin=origin,
                origin_reference=origin_reference[:128],
                safe_failure_code=str(failure_code),
                safe_failure_reason=failure_reason,
                internal_failure_detail=(validation_error.technical_detail if validation_error else ""),
            )
            DocumentValidationAttempt.objects.create(
                submission=submission,
                attempt_number=1,
                requested_by=actor,
                validator_version=VALIDATOR_VERSION,
                result_code=str(failure_code or "STRUCTURAL_VALIDATION_PASSED"),
                validation_state=submission.validation_state,
                security_status=submission.security_status,
                malware_status=submission.malware_status,
                page_count=submission.page_count,
                extraction_status=submission.extraction_status,
                findings=submission.validation_findings,
                risk_flags=submission.risk_flags,
                signature_status=submission.signature_status,
                signature_summary=submission.signature_summary,
                external_uri_domains=submission.external_uri_domains,
                safe_failure_code=submission.safe_failure_code,
                safe_failure_reason=submission.safe_failure_reason,
                internal_failure_detail=submission.internal_failure_detail,
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
                    metadata={
                        "duplicate_status": duplicate_status,
                        "failure_code": str(failure_code),
                        "validation_attempt": 1,
                        "validator_version": VALIDATOR_VERSION,
                    },
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def revalidate_document(*, actor, submission_id, expected_version: int) -> DocumentSubmission:
    try:
        snapshot = DocumentSubmission.objects.select_related("organization", "submitted_by").get(pk=submission_id)
    except (DocumentSubmission.DoesNotExist, ValueError) as exc:
        raise DocumentIntakeError("SUBMISSION_NOT_FOUND", "Document submission not found.") from exc
    authorized = can_submit_document(actor, snapshot.organization_id) and (
        snapshot.submitted_by_id == getattr(actor, "pk", None)
        or can_approve_document(actor, snapshot.organization_id)
    )
    if not authorized:
        raise DocumentIntakeError("REVALIDATION_NOT_AUTHORIZED", "You are not authorized to revalidate this submission.")
    if snapshot.duplicate_status in {
        DuplicateStatus.EXACT_PENDING_DUPLICATE,
        DuplicateStatus.EXACT_CORPUS_DUPLICATE,
    }:
        raise DocumentIntakeError("REVALIDATION_NOT_APPLICABLE", "Exact duplicate decisions are not changed by PDF revalidation.")
    if snapshot.version != expected_version:
        raise DocumentIntakeError("STALE_REVALIDATION", "The submission changed; refresh before revalidating.")
    if snapshot.state != SubmissionState.VALIDATION_FAILED:
        raise DocumentIntakeError("STALE_REVALIDATION", "The submission changed; refresh before revalidating.")
    path = quarantine_path(snapshot)
    if not path.is_file():
        raise DocumentIntakeError("QUARANTINE_FILE_MISSING", "The quarantined PDF is missing.")
    if _sha256_file(path) != snapshot.sha256:
        raise DocumentIntakeError("QUARANTINE_DIGEST_MISMATCH", "The quarantined PDF no longer matches its recorded digest.")

    validation_error: DocumentValidationError | None = None
    try:
        validated = validate_pdf(
            path,
            original_filename=snapshot.original_filename,
            declared_mime=snapshot.declared_mime,
            department=snapshot.organization.name,
        )
    except DocumentValidationError as exc:
        validation_error = exc
        validated = None

    role = primary_access_role(actor)
    try:
        with transaction.atomic():
            submission = DocumentSubmission.objects.select_for_update().select_related("organization").get(pk=snapshot.pk)
            if submission.version != expected_version or submission.state != SubmissionState.VALIDATION_FAILED:
                raise DocumentIntakeError("STALE_REVALIDATION", "The submission changed; refresh before revalidating.")
            if not can_revalidate_submission(actor, submission):
                raise DocumentIntakeError("REVALIDATION_NOT_AUTHORIZED", "You are not authorized to revalidate this submission.")
            attempt_number = submission.validation_attempts.count() + 1
            previous_state = submission.state
            record_governance_event(
                category=AuditCategory.DOCUMENT,
                action=AuditAction.DOCUMENT_REVALIDATION_REQUESTED,
                outcome=AuditOutcome.SUCCESS,
                actor=actor,
                actor_role=role,
                organization_id=submission.organization_id,
                resource_type=SUBJECT_TYPE,
                resource_id=str(submission.pk),
                resource_version=submission.version,
                correlation_id=submission.correlation_id,
                reason_code="REVALIDATION_REQUESTED",
                previous_state=previous_state,
                new_state=previous_state,
                metadata={"validation_attempt": attempt_number, "validator_version": VALIDATOR_VERSION},
            )

            inspection = validation_error.inspection if validation_error else None
            if validation_error:
                submission.state = SubmissionState.VALIDATION_FAILED
                submission.validation_state = ValidationState.FAILED
                submission.security_status = SecurityStatus.FAILED
                submission.malware_status = inspection.malware_status
                submission.page_count = inspection.page_count
                submission.extraction_status = inspection.extraction_status
                submission.preview_text = ""
                submission.validation_findings = validation_error.findings
                submission.risk_flags = inspection.risk_flags
                submission.signature_status = inspection.signature_status
                submission.signature_summary = inspection.signature_summary
                submission.external_uri_domains = inspection.external_uri_domains
                submission.safe_failure_code = validation_error.code
                submission.safe_failure_reason = validation_error.safe_message
                submission.internal_failure_detail = validation_error.technical_detail
                result_code = validation_error.code
            else:
                submission.state = SubmissionState.PENDING_IAS_APPROVAL
                submission.validation_state = ValidationState.PASSED
                submission.security_status = SecurityStatus.PASSED
                submission.malware_status = validated.malware_status
                submission.page_count = validated.page_count
                submission.extraction_status = validated.extraction_status
                submission.preview_text = validated.preview_text
                submission.detected_metadata = validated.detected_metadata
                submission.validation_findings = validated.findings
                submission.risk_flags = validated.risk_flags
                submission.signature_status = validated.signature_status
                submission.signature_summary = validated.signature_summary
                submission.external_uri_domains = validated.external_uri_domains
                submission.safe_failure_code = ""
                submission.safe_failure_reason = ""
                submission.internal_failure_detail = ""
                result_code = "STRUCTURAL_VALIDATION_PASSED"
            submission.validator_version = VALIDATOR_VERSION
            submission.version += 1
            submission.save(update_fields=(
                "state", "validation_state", "security_status", "malware_status", "page_count",
                "extraction_status", "preview_text", "detected_metadata", "validation_findings",
                "risk_flags", "validator_version", "signature_status", "signature_summary",
                "external_uri_domains", "safe_failure_code", "safe_failure_reason",
                "internal_failure_detail", "version", "updated_at",
            ))
            DocumentValidationAttempt.objects.create(
                submission=submission,
                attempt_number=attempt_number,
                requested_by=actor,
                validator_version=VALIDATOR_VERSION,
                result_code=result_code,
                validation_state=submission.validation_state,
                security_status=submission.security_status,
                malware_status=submission.malware_status,
                page_count=submission.page_count,
                extraction_status=submission.extraction_status,
                findings=submission.validation_findings,
                risk_flags=submission.risk_flags,
                signature_status=submission.signature_status,
                signature_summary=submission.signature_summary,
                external_uri_domains=submission.external_uri_domains,
                safe_failure_code=submission.safe_failure_code,
                safe_failure_reason=submission.safe_failure_reason,
                internal_failure_detail=submission.internal_failure_detail,
            )
            if validation_error:
                record_governance_event(
                    category=AuditCategory.SECURITY,
                    action=AuditAction.DOCUMENT_VALIDATION_FAILED,
                    outcome=AuditOutcome.FAILURE,
                    actor=actor,
                    actor_role=role,
                    organization_id=submission.organization_id,
                    resource_type=SUBJECT_TYPE,
                    resource_id=str(submission.pk),
                    resource_version=submission.version,
                    correlation_id=submission.correlation_id,
                    reason_code=validation_error.code,
                    reason=validation_error.safe_message,
                    previous_state=previous_state,
                    new_state=submission.state,
                    metadata={
                        "failure_code": validation_error.code,
                        "validation_attempt": attempt_number,
                        "validator_version": VALIDATOR_VERSION,
                    },
                )
                return submission

            record_governance_event(
                category=AuditCategory.DOCUMENT,
                action=AuditAction.DOCUMENT_VALIDATION_PASSED,
                outcome=AuditOutcome.SUCCESS,
                actor=actor,
                actor_role=role,
                organization_id=submission.organization_id,
                resource_type=SUBJECT_TYPE,
                resource_id=str(submission.pk),
                resource_version=submission.version,
                correlation_id=submission.correlation_id,
                reason_code="VALIDATION_PASSED",
                previous_state=previous_state,
                new_state=submission.state,
                metadata={
                    "page_count": submission.page_count,
                    "signature_status": submission.signature_status,
                    "validation_attempt": attempt_number,
                    "validator_version": VALIDATOR_VERSION,
                },
            )
            case = create_approval_case(
                workflow_type=WORKFLOW,
                organization=submission.organization,
                submitted_by=submission.submitted_by,
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
                organization_id=submission.organization_id,
                resource_type=SUBJECT_TYPE,
                resource_id=str(submission.pk),
                resource_version=submission.version,
                correlation_id=submission.correlation_id,
                reason_code="VALIDATION_PASSED",
                previous_state=previous_state,
                new_state=submission.state,
                metadata={"page_count": submission.page_count, "validation_attempt": attempt_number},
            )
            return submission
    except DocumentSubmission.DoesNotExist as exc:
        raise DocumentIntakeError("SUBMISSION_NOT_FOUND", "Document submission not found.") from exc
