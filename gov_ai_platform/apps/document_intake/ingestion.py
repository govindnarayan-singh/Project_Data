from __future__ import annotations

import hashlib
import os
import shutil
import socket
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.governance.audit import record_governance_event
from apps.governance.constants import (
    ApprovalCaseState,
    ApprovalDecisionType,
    AuditAction,
    AuditCategory,
    AuditOutcome,
)
from apps.identity_access.models import AccessRole
from knowledge_engine.documents.sources import APPROVED_SUBMISSION
from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline
from knowledge_engine.pipelines.batch_ingest import BatchIngestor, COMPLETED_STATUSES, ManifestEntry
from knowledge_engine.settings import settings as engine_settings

from .governance import SUBJECT_TYPE, WORKFLOW
from .models import (
    DocumentIngestionJob,
    DocumentIngestionWorkerHeartbeat,
    DocumentSubmission,
    DuplicateStatus,
    JobState,
    SecurityStatus,
    SubmissionOrigin,
    SubmissionState,
    ValidationState,
)
from .services import DocumentIntakeError, quarantine_path


class IngestionFailure(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, retryable: bool, detail: str = ""):
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.detail = detail
        super().__init__(safe_message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def worker_identifier() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def heartbeat_worker(*, worker_id: str, job_id=None) -> None:
    """Record worker liveness and renew only the lease owned by this worker."""
    now = timezone.now()
    with transaction.atomic():
        heartbeat, created = DocumentIngestionWorkerHeartbeat.objects.select_for_update().get_or_create(
            worker_id=worker_id,
            defaults={"started_at": now, "last_heartbeat": now, "current_job_id": job_id},
        )
        if not created:
            heartbeat.last_heartbeat = now
            heartbeat.current_job_id = job_id
            heartbeat.save(update_fields=("last_heartbeat", "current_job",))
        if job_id is not None:
            DocumentIngestionJob.objects.filter(
                pk=job_id,
                state=JobState.RUNNING,
                worker_id=worker_id,
            ).update(
                lease_expires_at=now + timedelta(seconds=settings.DOCUMENT_INGESTION_LEASE_SECONDS),
                updated_at=now,
            )


def remove_worker_heartbeat(*, worker_id: str) -> None:
    """Remove a cleanly stopped worker; crashed workers remain and become stale."""
    DocumentIngestionWorkerHeartbeat.objects.filter(worker_id=worker_id).delete()


def worker_health(*, job: DocumentIngestionJob | None = None) -> dict[str, object]:
    heartbeat = None
    if job and job.worker_id:
        heartbeat = DocumentIngestionWorkerHeartbeat.objects.filter(worker_id=job.worker_id).first()
    if heartbeat is None:
        heartbeat = DocumentIngestionWorkerHeartbeat.objects.order_by("-last_heartbeat").first()
    if heartbeat is None:
        return {"status": "NO_HEARTBEAT", "label": "No worker heartbeat", "last_heartbeat": None}
    freshness = max(settings.DOCUMENT_INGESTION_HEARTBEAT_SECONDS * 3, 90)
    online = heartbeat.last_heartbeat >= timezone.now() - timedelta(seconds=freshness)
    return {
        "status": "ONLINE" if online else "STALE",
        "label": "Online" if online else "No recent heartbeat",
        "worker_id": heartbeat.worker_id,
        "started_at": heartbeat.started_at,
        "last_heartbeat": heartbeat.last_heartbeat,
        "current_job_id": heartbeat.current_job_id,
    }


def _set_stage(job_id, stage: str) -> None:
    DocumentIngestionJob.objects.filter(pk=job_id, state=JobState.RUNNING).update(
        current_stage=stage[:64],
        updated_at=timezone.now(),
    )


def _validate_ingestion_gate(job: DocumentIngestionJob, submission: DocumentSubmission) -> None:
    if submission.state != SubmissionState.INGESTING:
        raise IngestionFailure("APPROVAL_GATE_FAILED", "The submission is not in the approved ingestion state.", retryable=False)
    if submission.validation_state != ValidationState.PASSED or submission.security_status != SecurityStatus.PASSED:
        raise IngestionFailure("VALIDATION_GATE_FAILED", "The latest document validation does not permit ingestion.", retryable=False)
    latest = submission.validation_attempts.order_by("-attempt_number").first()
    if latest is None or latest.validation_state != ValidationState.PASSED or latest.security_status != SecurityStatus.PASSED:
        raise IngestionFailure("VALIDATION_GATE_FAILED", "The latest document validation does not permit ingestion.", retryable=False)
    if submission.duplicate_status in {DuplicateStatus.EXACT_PENDING_DUPLICATE}:
        raise IngestionFailure("DUPLICATE_GATE_FAILED", "The submission duplicates another active submission.", retryable=False)
    case = submission.approval_case
    if (
        case is None
        or case.workflow_type != WORKFLOW
        or case.state != ApprovalCaseState.APPROVED
        or case.subject_type != SUBJECT_TYPE
        or case.subject_id != str(submission.pk)
        or case.subject_digest != submission.sha256
        or case.organization_id != submission.organization_id
        or not case.decisions.filter(decision=ApprovalDecisionType.APPROVE).exists()
    ):
        raise IngestionFailure("APPROVAL_GATE_FAILED", "Final IAS corpus-admission approval could not be verified.", retryable=False)
    expected_key = f"document-admission:{submission.pk}:{submission.sha256}"
    if job.submission_id != submission.pk or job.idempotency_key != expected_key:
        raise IngestionFailure("JOB_IDENTITY_MISMATCH", "The ingestion job identity is invalid.", retryable=False)


def approved_archive_path(submission: DocumentSubmission) -> tuple[str, Path]:
    key = f"{submission.pk}/original.pdf"
    root = Path(settings.DOCUMENT_APPROVED_ROOT).resolve()
    candidate = (root / key).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise IngestionFailure("ARCHIVE_PROMOTION_FAILED", "The approved archive path is invalid.", retryable=False) from exc
    return key, candidate


def _promote(submission: DocumentSubmission) -> tuple[str, Path]:
    source = quarantine_path(submission)
    if not source.is_file():
        raise IngestionFailure("QUARANTINE_FILE_MISSING", "The quarantined PDF is missing.", retryable=False)
    if _sha256(source) != submission.sha256:
        raise IngestionFailure("BYTE_INTEGRITY_MISMATCH", "The uploaded file changed after approval and was blocked for safety.", retryable=False)
    key, destination = approved_archive_path(submission)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _sha256(destination) != submission.sha256:
            raise IngestionFailure("ARCHIVE_PROMOTION_FAILED", "An archive integrity conflict was detected.", retryable=False)
        return key, destination
    temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _sha256(temporary) != submission.sha256:
            raise IngestionFailure("ARCHIVE_PROMOTION_FAILED", "The archive copy failed integrity verification.", retryable=True)
        os.replace(temporary, destination)
    except IngestionFailure:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise IngestionFailure("ARCHIVE_PROMOTION_FAILED", "The approved archive could not be written.", retryable=True, detail=str(exc)) from exc
    return key, destination


def _recover_expired_jobs(now) -> None:
    expired = list(DocumentIngestionJob.objects.select_for_update().filter(state=JobState.RUNNING, lease_expires_at__lt=now))
    for job in expired:
        retryable = job.attempt_count < job.max_attempts
        job.state = JobState.RETRYABLE if retryable else JobState.FAILED
        job.last_error_code = "WORKER_LEASE_EXPIRED"
        job.safe_error_reason = "The ingestion worker stopped before completing the job."
        job.worker_id = ""
        job.current_stage = "FAILED"
        job.lease_expires_at = None
        job.finished_at = now if not retryable else None
        job.version += 1
        job.save(update_fields=("state", "last_error_code", "safe_error_reason", "worker_id", "current_stage", "lease_expires_at", "finished_at", "version", "updated_at"))
        submission = job.submission
        submission.state = SubmissionState.INGESTION_FAILED
        submission.safe_failure_code = job.last_error_code
        submission.safe_failure_reason = job.safe_error_reason
        submission.version += 1
        submission.save(update_fields=("state", "safe_failure_code", "safe_failure_reason", "version", "updated_at"))
        record_governance_event(
            category=AuditCategory.DOCUMENT,
            action=AuditAction.DOCUMENT_INGESTION_FAILED,
            outcome=AuditOutcome.FAILURE,
            organization_id=submission.organization_id,
            resource_type="document_intake.DocumentIngestionJob",
            resource_id=str(job.pk),
            resource_version=job.version,
            correlation_id=submission.correlation_id,
            reason_code=job.last_error_code,
            reason=job.safe_error_reason,
            previous_state=JobState.RUNNING,
            new_state=job.state,
            metadata={"failure_code": job.last_error_code, "ingestion_attempt": job.attempt_count},
        )


def claim_ingestion_job(*, job_id=None, worker_id: str | None = None) -> DocumentIngestionJob | None:
    worker_id = (worker_id or worker_identifier())[:128]
    now = timezone.now()
    with transaction.atomic():
        _recover_expired_jobs(now)
        queryset = (
            DocumentIngestionJob.objects.select_for_update(skip_locked=True)
            .select_related("submission__approval_case")
            .filter(
                state__in=(JobState.PENDING, JobState.RETRYABLE),
                attempt_count__lt=F("max_attempts"),
                submission__state__in=(SubmissionState.QUEUED_FOR_INGESTION, SubmissionState.INGESTION_FAILED),
                submission__validation_state=ValidationState.PASSED,
                submission__security_status=SecurityStatus.PASSED,
                submission__approval_case__workflow_type=WORKFLOW,
                submission__approval_case__state=ApprovalCaseState.APPROVED,
                submission__approval_case__decisions__decision=ApprovalDecisionType.APPROVE,
            )
        )
        if job_id is not None:
            queryset = queryset.filter(pk=job_id)
        job = queryset.order_by("created_at").first()
        if job is None:
            return None
        job.state = JobState.RUNNING
        job.attempt_count += 1
        job.worker_id = worker_id
        job.current_stage = "CLAIMED"
        job.started_at = now
        job.lease_expires_at = now + timedelta(seconds=settings.DOCUMENT_INGESTION_LEASE_SECONDS)
        job.finished_at = None
        job.version += 1
        job.save(update_fields=("state", "attempt_count", "worker_id", "current_stage", "started_at", "lease_expires_at", "finished_at", "version", "updated_at"))
        submission = DocumentSubmission.objects.select_for_update().get(pk=job.submission_id)
        previous_submission_state = submission.state
        submission.state = SubmissionState.INGESTING
        submission.version += 1
        submission.save(update_fields=("state", "version", "updated_at"))
        record_governance_event(
            category=AuditCategory.DOCUMENT,
            action=AuditAction.DOCUMENT_INGESTION_STARTED,
            outcome=AuditOutcome.SUCCESS,
            organization_id=submission.organization_id,
            resource_type="document_intake.DocumentIngestionJob",
            resource_id=str(job.pk),
            resource_version=job.version,
            correlation_id=submission.correlation_id,
            reason_code="WORKER_CLAIMED",
            previous_state=previous_submission_state,
            new_state=JobState.RUNNING,
            metadata={"ingestion_attempt": job.attempt_count},
        )
        return job


def _classify_batch_failure(result) -> IngestionFailure:
    stage = str(result.error_stage or "").casefold()
    detail = str(result.error_message or "")[:2000]
    if "embedding" in stage:
        return IngestionFailure("EMBEDDING_FAILED", "Embedding generation failed.", retryable=True, detail=detail)
    if "database" in stage or "identity" in stage:
        return IngestionFailure("DATABASE_WRITE_FAILED", "Database ingestion failed.", retryable=True, detail=detail)
    if "tesseract" in detail.casefold() or "ocr" in detail.casefold():
        return IngestionFailure("OCR_FAILED", "OCR processing failed.", retryable=True, detail=detail)
    return IngestionFailure("EXTRACTION_FAILED", "Document extraction or metadata processing failed.", retryable=False, detail=detail)


def _finish_failure(job_id, failure: IngestionFailure) -> DocumentIngestionJob:
    now = timezone.now()
    with transaction.atomic():
        job = DocumentIngestionJob.objects.select_for_update().select_related("submission").get(pk=job_id)
        retryable = failure.retryable and job.attempt_count < job.max_attempts
        previous = job.state
        job.state = JobState.RETRYABLE if retryable else JobState.FAILED
        job.last_error_code = failure.code
        job.safe_error_reason = failure.safe_message
        job.internal_error_detail = failure.detail[:4000]
        job.current_stage = "FAILED"
        job.lease_expires_at = None
        job.finished_at = None if retryable else now
        job.version += 1
        job.save(update_fields=("state", "last_error_code", "safe_error_reason", "internal_error_detail", "current_stage", "lease_expires_at", "finished_at", "version", "updated_at"))
        submission = DocumentSubmission.objects.select_for_update().get(pk=job.submission_id)
        submission.state = SubmissionState.INGESTION_FAILED
        submission.safe_failure_code = failure.code
        submission.safe_failure_reason = failure.safe_message
        submission.internal_failure_detail = failure.detail[:4000]
        submission.version += 1
        submission.save(update_fields=("state", "safe_failure_code", "safe_failure_reason", "internal_failure_detail", "version", "updated_at"))
        record_governance_event(
            category=AuditCategory.SECURITY if failure.code == "BYTE_INTEGRITY_MISMATCH" else AuditCategory.DOCUMENT,
            action=AuditAction.DOCUMENT_INGESTION_FAILED,
            outcome=AuditOutcome.FAILURE,
            organization_id=submission.organization_id,
            resource_type="document_intake.DocumentIngestionJob",
            resource_id=str(job.pk),
            resource_version=job.version,
            correlation_id=submission.correlation_id,
            reason_code=failure.code,
            reason=failure.safe_message,
            previous_state=previous,
            new_state=job.state,
            metadata={"failure_code": failure.code, "ingestion_attempt": job.attempt_count},
        )
        return job


def process_ingestion_job(job: DocumentIngestionJob, *, ingestor=None, graph_pipeline=None) -> DocumentIngestionJob:
    try:
        submission = DocumentSubmission.objects.select_related("organization", "approval_case").get(pk=job.submission_id)
        if job.state != JobState.RUNNING:
            if job.state == JobState.SUCCEEDED:
                return job
            raise IngestionFailure("INVALID_JOB_STATE", "The ingestion job is not running.", retryable=False)
        _set_stage(job.pk, "VERIFYING_APPROVAL")
        _validate_ingestion_gate(job, submission)
        _set_stage(job.pk, "VERIFYING_BYTES")
        archive_key, archive_path = _promote(submission)
        _set_stage(job.pk, "ARCHIVED")
        with transaction.atomic():
            promoted = DocumentSubmission.objects.select_for_update().get(pk=submission.pk)
            if promoted.archive_key != archive_key:
                promoted.archive_key = archive_key
                promoted.version += 1
                promoted.save(update_fields=("archive_key", "version", "updated_at"))
        run_id = f"stage3-{job.pk}-{job.attempt_count}"
        entry = ManifestEntry(
            row_number=1,
            file_path=archive_key,
            department=submission.organization.name,
            document_label=str(submission.detected_metadata.get("subject") or submission.original_filename),
            notes=f"Stage 3 submission {submission.pk}",
            absolute_path=archive_path,
            source_origin=APPROVED_SUBMISSION,
            source_authority=submission.source_provenance,
            canonical_identity=str(submission.detected_metadata.get("gr_number") or submission.claimed_identifier or ""),
            original_acquisition_path=submission.storage_key,
            archive_source_root="APPROVED_SUBMISSION",
            archive_relative_path=archive_key,
            archived_sha256=submission.sha256,
            archived_at=timezone.now(),
        )
        active_ingestor = ingestor or BatchIngestor()
        _set_stage(job.pk, "PROCESSING_DOCUMENT")
        result = active_ingestor.process_entry(entry, run_id=run_id)
        if result.status not in COMPLETED_STATUSES:
            raise _classify_batch_failure(result)
        document_id = int(result.document_id)
        if not document_id:
            raise IngestionFailure("DATABASE_WRITE_FAILED", "The ingestion result did not identify a document.", retryable=True)
        if isinstance(active_ingestor, BatchIngestor):
            try:
                integrity = active_ingestor.storage.validate_document_integrity(
                    document_id,
                    max_tokens=engine_settings.chunk_max_tokens,
                    embedding_dimension=engine_settings.embed_dim,
                )
            except Exception as exc:
                raise IngestionFailure(
                    "DATABASE_WRITE_FAILED",
                    "Stored document integrity verification failed.",
                    retryable=True,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            if integrity["pages"] <= 0 or integrity["chunks"] <= 0:
                raise IngestionFailure("DATABASE_WRITE_FAILED", "The stored document has no searchable content.", retryable=True)
        else:
            integrity = {
                "document_id": document_id,
                "pages": int(getattr(result, "pages_total", 0) or 0),
                "chunks": int(getattr(result, "chunks_created", 0) or 0),
                "embedding_dimension": int(getattr(result, "embedding_dimension", 0) or 0),
            }
        pipeline_result = {
            "status": str(result.status),
            "operation": str(getattr(result, "operation", "") or ""),
            "pages": integrity["pages"],
            "chunks": integrity["chunks"],
            "embeddings": integrity["chunks"],
            "embedding_dimension": integrity["embedding_dimension"],
            "pages_digital": int(getattr(result, "pages_digital", 0) or 0),
            "pages_ocr": int(getattr(result, "pages_ocr", 0) or 0),
            "extraction_method": str(getattr(result, "extraction_method", "") or ""),
            "duplicate_result": "EXACT_ALREADY_INGESTED" if str(getattr(result, "operation", "")) == "SKIPPED_UNCHANGED" else "NEW_DOCUMENT",
        }
        try:
            _set_stage(job.pk, "UPDATING_KNOWLEDGE_GRAPH")
            kg_result = (graph_pipeline or KnowledgeGraphPipeline()).run_targeted(document_id)
        except Exception as exc:
            raise IngestionFailure("KG_RESOLUTION_FAILED", "Targeted knowledge-graph processing failed.", retryable=True, detail=f"{type(exc).__name__}: {exc}") from exc
        result_summary = {**kg_result, "pipeline": pipeline_result}
        now = timezone.now()
        with transaction.atomic():
            current = DocumentIngestionJob.objects.select_for_update().get(pk=job.pk)
            submission = DocumentSubmission.objects.select_for_update().get(pk=current.submission_id)
            current.state = JobState.SUCCEEDED
            current.ingestion_run_id = run_id
            current.kg_result = result_summary
            current.last_error_code = ""
            current.safe_error_reason = ""
            current.internal_error_detail = ""
            current.current_stage = "COMPLETED"
            current.lease_expires_at = None
            current.finished_at = now
            current.version += 1
            current.save(update_fields=("state", "ingestion_run_id", "kg_result", "last_error_code", "safe_error_reason", "internal_error_detail", "current_stage", "lease_expires_at", "finished_at", "version", "updated_at"))
            submission.state = SubmissionState.INGESTED
            submission.archive_key = archive_key
            submission.ingested_document_id = document_id
            submission.page_count = integrity["pages"]
            submission.extraction_status = str(result.status)
            submission.safe_failure_code = ""
            submission.safe_failure_reason = ""
            submission.version += 1
            submission.save(update_fields=("state", "archive_key", "ingested_document_id", "page_count", "extraction_status", "safe_failure_code", "safe_failure_reason", "version", "updated_at"))
            if submission.origin == SubmissionOrigin.UNRESOLVED_REFERENCE:
                from apps.knowledge_graph_ui.workbench_services import refresh_linked_work_item_for_submission

                refresh_linked_work_item_for_submission(submission)
            record_governance_event(
                category=AuditCategory.DOCUMENT,
                action=AuditAction.DOCUMENT_INGESTION_SUCCEEDED,
                outcome=AuditOutcome.SUCCESS,
                organization_id=submission.organization_id,
                resource_type="document_intake.DocumentIngestionJob",
                resource_id=str(current.pk),
                resource_version=current.version,
                correlation_id=submission.correlation_id,
                reason_code="INGESTION_SUCCEEDED",
                previous_state=JobState.RUNNING,
                new_state=JobState.SUCCEEDED,
                metadata={"ingestion_attempt": current.attempt_count},
            )
            return current
    except IngestionFailure as exc:
        return _finish_failure(job.pk, exc)
    except Exception as exc:
        return _finish_failure(job.pk, IngestionFailure(
            "DATABASE_WRITE_FAILED",
            "The ingestion worker encountered an internal failure.",
            retryable=True,
            detail=f"{type(exc).__name__}: {exc}",
        ))


def retry_ingestion(*, actor, submission_id, expected_version: int) -> DocumentIngestionJob:
    from apps.identity_access.permissions import can_approve_document

    with transaction.atomic():
        submission = DocumentSubmission.objects.select_for_update().get(pk=submission_id)
        if not can_approve_document(actor, submission.organization_id):
            raise DocumentIntakeError("OUT_OF_SCOPE", "IAS authority is required to retry this ingestion.")
        if submission.version != expected_version:
            raise DocumentIntakeError("STALE_SUBMISSION", "The submission changed; refresh before retrying.")
        job = DocumentIngestionJob.objects.select_for_update().get(submission=submission)
        if job.state != JobState.RETRYABLE or job.attempt_count >= job.max_attempts:
            raise DocumentIntakeError("NOT_RETRYABLE", "This ingestion job is not retryable.")
        previous = job.state
        job.state = JobState.PENDING
        job.worker_id = ""
        job.current_stage = "WAITING"
        job.lease_expires_at = None
        job.version += 1
        job.save(update_fields=("state", "worker_id", "current_stage", "lease_expires_at", "version", "updated_at"))
        submission.state = SubmissionState.QUEUED_FOR_INGESTION
        submission.safe_failure_code = ""
        submission.safe_failure_reason = ""
        submission.version += 1
        submission.save(update_fields=("state", "safe_failure_code", "safe_failure_reason", "version", "updated_at"))
        record_governance_event(
            category=AuditCategory.DOCUMENT,
            action=AuditAction.DOCUMENT_INGESTION_RETRY_REQUESTED,
            outcome=AuditOutcome.SUCCESS,
            actor=actor,
            actor_role=AccessRole.IAS_OFFICER,
            organization_id=submission.organization_id,
            resource_type="document_intake.DocumentIngestionJob",
            resource_id=str(job.pk),
            resource_version=job.version,
            correlation_id=submission.correlation_id,
            reason_code="IAS_RETRY_REQUESTED",
            previous_state=previous,
            new_state=job.state,
            metadata={"ingestion_attempt": job.attempt_count},
        )
        return job
