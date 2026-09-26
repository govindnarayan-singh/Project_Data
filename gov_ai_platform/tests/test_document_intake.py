from __future__ import annotations

import hashlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from apps.document_intake.ingestion import (
    claim_ingestion_job,
    process_ingestion_job,
    retry_ingestion,
)
from apps.document_intake.models import (
    DocumentIngestionJob,
    DocumentSubmission,
    DocumentValidationAttempt,
    DuplicateStatus,
    JobState,
    MalwareStatus,
    SecurityStatus,
    SourceProvenance,
    SubmissionState,
    ValidationState,
)
from apps.document_intake.services import DocumentIntakeError, quarantine_path, submit_document
from apps.governance.audit import verify_audit_chain
from apps.governance.constants import GENESIS_HASH, ApprovalDecisionType, AuditAction
from apps.governance.exceptions import AuditWriteError, GovernanceApprovalError
from apps.governance.models import GovernanceApprovalDecision, GovernanceAuditChain, GovernanceAuditEvent
from apps.governance.services import decide_approval_case
from apps.identity_access.models import (
    AccessGrant,
    AccessGrantState,
    AccessRole,
    IASAuthorityScope,
    OrganizationUnit,
)


def pdf_bytes(text: str = "Government Resolution Number TEST/2026/1") -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(320, 420))
    document.drawString(30, 370, text)
    document.drawString(30, 345, "Government of Maharashtra")
    document.showPage()
    document.save()
    return output.getvalue()


def encrypted_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=320, height=420)
    writer.encrypt("test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class RawUpload:
    def __init__(self, name: str, content: bytes, content_type: str = "application/pdf"):
        self.name = name
        self.content = content
        self.content_type = content_type

    def chunks(self):
        yield self.content


class DocumentIntakeBase(TestCase):
    def setUp(self):
        super().setUp()
        tmp_root = Path(settings.BASE_DIR) / "tmp"
        tmp_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=tmp_root, prefix="stage3-test-")
        self.runtime_root = Path(self.temporary.name)
        self.override = override_settings(
            DOCUMENT_QUARANTINE_ROOT=self.runtime_root / "Incoming" / "Officer_Uploads",
            DOCUMENT_APPROVED_ROOT=self.runtime_root / "Approved_Submissions",
            DOCUMENT_MALWARE_SCANNER="",
            DOCUMENT_MALWARE_SCANNER_EXECUTABLE="",
            DOCUMENT_MAX_UPLOAD_BYTES=1024 * 1024,
            DOCUMENT_MAX_PAGES=10,
            DOCUMENT_PREVIEW_PAGES=2,
            DOCUMENT_PREVIEW_CHARACTERS=1200,
        )
        self.override.enable()
        GovernanceAuditChain.objects.get_or_create(
            pk=1,
            defaults={"last_sequence": 0, "head_hash": GENESIS_HASH},
        )
        self.org_a = OrganizationUnit.objects.create(code="stage3-a", name="Stage 3 A")
        self.org_b = OrganizationUnit.objects.create(code="stage3-b", name="Stage 3 B")
        users = get_user_model()
        self.officer = users.objects.create_user("stage3-officer", "stage3-officer@example.gov", "TestOnlyPass-9321")
        self.other_officer = users.objects.create_user("stage3-other", "stage3-other@example.gov", "TestOnlyPass-9321")
        self.ias = users.objects.create_user("stage3-ias", "stage3-ias@example.gov", "TestOnlyPass-9321")
        self.wrong_ias = users.objects.create_user("stage3-ias-b", "stage3-ias-b@example.gov", "TestOnlyPass-9321")
        self.pending = users.objects.create_user("stage3-pending", "stage3-pending@example.gov", "TestOnlyPass-9321")
        AccessGrant.objects.create(user=self.officer, organization=self.org_a, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.other_officer, organization=self.org_a, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.ias, organization=self.org_a, role=AccessRole.IAS_OFFICER)
        AccessGrant.objects.create(user=self.wrong_ias, organization=self.org_b, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.org_a)
        IASAuthorityScope.objects.create(user=self.wrong_ias, organization=self.org_b)

    def tearDown(self):
        self.override.disable()
        self.temporary.cleanup()
        super().tearDown()

    def upload(self, text="Government Resolution TEST/2026/1", *, user=None, organization=None):
        return submit_document(
            actor=user or self.officer,
            upload=SimpleUploadedFile("government.pdf", pdf_bytes(text), content_type="application/pdf"),
            organization=organization or self.org_a,
            claimed_identifier="TEST/2026/1",
            claimed_source_url="https://example.gov/document.pdf",
            source_provenance=SourceProvenance.IAS_VERIFIED_COPY,
            submission_reason="Potentially useful government record.",
        )


class DocumentUploadSecurityTests(DocumentIntakeBase):
    def test_authorization_scope_and_anonymous_boundaries(self):
        self.assertEqual(self.client.get(reverse("document_intake:submit")).status_code, 302)
        self.client.force_login(self.pending)
        self.assertNotEqual(self.client.get(reverse("document_intake:submit")).status_code, 200)
        suspended = get_user_model().objects.create_user("stage3-suspended", password="TestOnlyPass-9321")
        AccessGrant.objects.create(
            user=suspended,
            organization=self.org_a,
            role=AccessRole.OFFICER,
            state=AccessGrantState.SUSPENDED,
        )
        self.client.force_login(suspended)
        self.assertNotEqual(self.client.get(reverse("document_intake:submit")).status_code, 200)
        revoked = get_user_model().objects.create_user("stage3-revoked", password="TestOnlyPass-9321")
        AccessGrant.objects.create(
            user=revoked,
            organization=self.org_a,
            role=AccessRole.OFFICER,
            state=AccessGrantState.REVOKED,
        )
        self.client.force_login(revoked)
        self.assertNotEqual(self.client.get(reverse("document_intake:submit")).status_code, 200)
        self.client.force_login(self.officer)
        cross_scope = self.client.post(reverse("document_intake:submit"), {
            "document": SimpleUploadedFile("government.pdf", pdf_bytes(), content_type="application/pdf"),
            "organization": self.org_b.pk,
            "source_provenance": SourceProvenance.UNKNOWN,
            "submission_reason": "Cross scope attempt",
        })
        self.assertEqual(cross_scope.status_code, 200)
        self.assertEqual(DocumentSubmission.objects.count(), 0)
        valid = self.client.post(reverse("document_intake:submit"), {
            "document": SimpleUploadedFile("government.pdf", pdf_bytes(), content_type="application/pdf"),
            "organization": self.org_a.pk,
            "source_provenance": SourceProvenance.IAS_VERIFIED_COPY,
            "submission_reason": "Own scope submission",
        })
        self.assertEqual(valid.status_code, 302)
        submission = DocumentSubmission.objects.get()
        self.assertEqual(submission.source_provenance, SourceProvenance.OFFICER_PROVIDED_UNVERIFIED)
        self.assertEqual(submission.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertIsNotNone(submission.approval_case_id)

    def test_pdf_validation_rejects_type_signature_empty_size_encryption_and_malformed(self):
        zero_page_writer = PdfWriter()
        zero_page = io.BytesIO()
        zero_page_writer.write(zero_page)
        cases = [
            ("not-pdf.txt", pdf_bytes(), "application/pdf", "UNSUPPORTED_EXTENSION"),
            ("fake.pdf", b"not a pdf", "application/pdf", "INVALID_PDF_SIGNATURE"),
            ("wrong-mime.pdf", pdf_bytes(), "text/plain", "MIME_MISMATCH"),
            ("empty.pdf", b"", "application/pdf", "EMPTY_FILE"),
            ("encrypted.pdf", encrypted_pdf_bytes(), "application/pdf", "ENCRYPTED_PDF"),
            ("malformed.pdf", b"%PDF-broken", "application/pdf", "MALFORMED_PDF"),
            ("zero-page.pdf", zero_page.getvalue(), "application/pdf", "ZERO_PAGE_PDF"),
        ]
        for index, (name, content, mime, code) in enumerate(cases):
            with self.subTest(code=code):
                submission = submit_document(
                    actor=self.officer,
                    upload=SimpleUploadedFile(name, content, content_type=mime),
                    organization=self.org_a,
                    claimed_identifier="",
                    claimed_source_url="",
                    source_provenance=SourceProvenance.UNKNOWN,
                    submission_reason=f"validation case {index}",
                )
                self.assertEqual(submission.state, SubmissionState.VALIDATION_FAILED)
                self.assertEqual(submission.safe_failure_code, code)
                self.assertFalse(submission.approval_case_id)
        with override_settings(DOCUMENT_MAX_UPLOAD_BYTES=10):
            with self.assertRaises(DocumentIntakeError) as oversized:
                submit_document(
                    actor=self.officer,
                    upload=SimpleUploadedFile("large.pdf", pdf_bytes(), content_type="application/pdf"),
                    organization=self.org_a,
                    claimed_identifier="",
                    claimed_source_url="",
                    source_provenance=SourceProvenance.UNKNOWN,
                    submission_reason="oversized",
                )
        self.assertEqual(oversized.exception.code, "FILE_TOO_LARGE")

        embedded_writer = PdfWriter()
        embedded_writer.add_blank_page(width=200, height=200)
        embedded_writer.add_attachment("embedded.txt", b"not permitted")
        embedded = io.BytesIO()
        embedded_writer.write(embedded)
        embedded_submission = submit_document(
            actor=self.officer,
            upload=SimpleUploadedFile("embedded.pdf", embedded.getvalue(), content_type="application/pdf"),
            organization=self.org_a,
            claimed_identifier="",
            claimed_source_url="",
            source_provenance=SourceProvenance.UNKNOWN,
            submission_reason="embedded file",
        )
        self.assertEqual(embedded_submission.safe_failure_code, "EMBEDDED_FILE_DETECTED")

        two_page_writer = PdfWriter()
        two_page_writer.add_blank_page(width=200, height=200)
        two_page_writer.add_blank_page(width=200, height=200)
        two_pages = io.BytesIO()
        two_page_writer.write(two_pages)
        with override_settings(DOCUMENT_MAX_PAGES=1):
            page_limited = submit_document(
                actor=self.officer,
                upload=SimpleUploadedFile("two-pages.pdf", two_pages.getvalue(), content_type="application/pdf"),
                organization=self.org_a,
                claimed_identifier="",
                claimed_source_url="",
                source_provenance=SourceProvenance.UNKNOWN,
                submission_reason="page limit",
            )
        self.assertEqual(page_limited.safe_failure_code, "PAGE_LIMIT_EXCEEDED")

    def test_exact_corpus_and_possible_identity_duplicates_are_explicit(self):
        with patch("apps.document_intake.services._corpus_match", return_value=(87, False)):
            exact = self.upload("Exact corpus duplicate TEST/2026/61")
        self.assertEqual(exact.duplicate_status, DuplicateStatus.EXACT_CORPUS_DUPLICATE)
        self.assertEqual(exact.state, SubmissionState.VALIDATION_FAILED)
        self.assertEqual(exact.matched_document_id, 87)
        self.assertIsNone(exact.approval_case_id)

        with patch("apps.document_intake.services._corpus_match", return_value=(42, True)):
            possible = self.upload("Possible identity duplicate TEST/2026/62")
        self.assertEqual(possible.duplicate_status, DuplicateStatus.POSSIBLE_IDENTITY_DUPLICATE)
        self.assertEqual(possible.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertEqual(possible.matched_document_id, 42)
        self.assertIsNotNone(possible.approval_case_id)

    def test_active_content_path_safety_duplicate_and_protected_download(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.add_js("app.alert('unsafe')")
        active = io.BytesIO()
        writer.write(active)
        rejected = submit_document(
            actor=self.officer,
            upload=SimpleUploadedFile("active.pdf", active.getvalue(), content_type="application/pdf"),
            organization=self.org_a,
            claimed_identifier="",
            claimed_source_url="",
            source_provenance=SourceProvenance.UNKNOWN,
            submission_reason="active content",
        )
        self.assertEqual(rejected.safe_failure_code, "JAVASCRIPT_DETECTED")

        duplicate_bytes = pdf_bytes("Government Resolution DUPLICATE/2026/7")
        first = submit_document(
            actor=self.officer,
            upload=SimpleUploadedFile("first.pdf", duplicate_bytes, content_type="application/pdf"),
            organization=self.org_a,
            claimed_identifier="DUPLICATE/2026/7",
            claimed_source_url="",
            source_provenance=SourceProvenance.UNKNOWN,
            submission_reason="first exact copy",
        )
        duplicate = submit_document(
            actor=self.officer,
            upload=SimpleUploadedFile("second.pdf", duplicate_bytes, content_type="application/pdf"),
            organization=self.org_a,
            claimed_identifier="DUPLICATE/2026/7",
            claimed_source_url="",
            source_provenance=SourceProvenance.UNKNOWN,
            submission_reason="second exact copy",
        )
        self.assertEqual(duplicate.duplicate_status, DuplicateStatus.EXACT_PENDING_DUPLICATE)
        self.assertEqual(duplicate.state, SubmissionState.VALIDATION_FAILED)
        self.assertEqual(duplicate.matched_submission_id, first.pk)

        traversal = submit_document(
            actor=self.officer,
            upload=RawUpload("../../outside.pdf", pdf_bytes("Safe path test")),
            organization=self.org_a,
            claimed_identifier="",
            claimed_source_url="",
            source_provenance=SourceProvenance.UNKNOWN,
            submission_reason="path normalization",
        )
        self.assertEqual(traversal.original_filename, "outside.pdf")
        self.assertEqual(quarantine_path(traversal).parent.name, str(traversal.pk))
        self.assertTrue(quarantine_path(traversal).is_relative_to(Path(settings.DOCUMENT_QUARANTINE_ROOT)))

        self.client.force_login(self.other_officer)
        self.assertEqual(self.client.get(reverse("document_intake:download", args=[first.pk])).status_code, 404)
        self.client.force_login(self.officer)
        response = self.client.get(reverse("document_intake:preview", args=[first.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Security-Policy"], "sandbox")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        for closer in response._resource_closers:
            closer()
        response._resource_closers.clear()

    def test_submission_is_audited_without_preview_content(self):
        marker = "PRIVATE-PREVIEW-CONTENT-STAGE3"
        submission = self.upload(marker)
        events = GovernanceAuditEvent.objects.filter(correlation_id=submission.correlation_id)
        self.assertSetEqual(
            set(events.values_list("action", flat=True)),
            {AuditAction.DOCUMENT_SUBMITTED, AuditAction.DOCUMENT_READY_FOR_APPROVAL, AuditAction.APPROVAL_CASE_CREATED},
        )
        serialized = json.dumps(list(events.values()), default=str)
        self.assertNotIn(marker, serialized)
        self.assertTrue(verify_audit_chain().valid)


class DocumentGovernanceTests(DocumentIntakeBase):
    def decide(self, submission, actor, decision=ApprovalDecisionType.APPROVE):
        return decide_approval_case(
            actor,
            submission.approval_case_id,
            decision=decision,
            reason="Evidence reviewed for corpus admission.",
            expected_case_version=submission.approval_case.version,
            expected_subject_version=submission.version,
            correlation_id=submission.correlation_id,
        )

    def test_officer_wrong_scope_stale_and_authorized_approval(self):
        submission = self.upload("Governance approval TEST/2026/20")
        with self.assertRaises(GovernanceApprovalError):
            self.decide(submission, self.officer)
        with self.assertRaises(GovernanceApprovalError):
            self.decide(submission, self.wrong_ias)
        submission.version += 1
        submission.save(update_fields=("version", "updated_at"))
        with self.assertRaisesMessage(GovernanceApprovalError, "subject changed"):
            self.decide(submission, self.ias)
        submission.version -= 1
        submission.save(update_fields=("version", "updated_at"))
        self.decide(submission, self.ias)
        submission.refresh_from_db()
        self.assertEqual(submission.state, SubmissionState.QUEUED_FOR_INGESTION)
        self.assertEqual(DocumentIngestionJob.objects.filter(submission=submission).count(), 1)
        with self.assertRaises(GovernanceApprovalError):
            self.decide(submission, self.ias)
        self.assertEqual(DocumentIngestionJob.objects.filter(submission=submission).count(), 1)
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_APPROVED, correlation_id=submission.correlation_id
        ).exists())

    def test_rejection_never_creates_ingestion_job(self):
        submission = self.upload("Governance rejection TEST/2026/21")
        self.decide(submission, self.ias, ApprovalDecisionType.REJECT)
        submission.refresh_from_db()
        self.assertEqual(submission.state, SubmissionState.REJECTED)
        self.assertFalse(DocumentIngestionJob.objects.filter(submission=submission).exists())
        self.assertEqual(submission.ingested_document_id, None)
        self.assertTrue(quarantine_path(submission).exists())

    def test_job_or_audit_failure_rolls_back_entire_approval(self):
        job_failure = self.upload("Approval job rollback TEST/2026/24")
        initial_events = GovernanceAuditEvent.objects.count()
        with patch("apps.document_intake.governance.DocumentIngestionJob.objects.create", side_effect=DatabaseError("forced")):
            with self.assertRaises(DatabaseError):
                self.decide(job_failure, self.ias)
        job_failure.refresh_from_db()
        job_failure.approval_case.refresh_from_db()
        self.assertEqual(job_failure.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertEqual(job_failure.approval_case.state, "PENDING")
        self.assertFalse(GovernanceApprovalDecision.objects.filter(case=job_failure.approval_case).exists())
        self.assertEqual(GovernanceAuditEvent.objects.count(), initial_events)

        audit_failure = self.upload("Approval audit rollback TEST/2026/25")
        with patch("apps.document_intake.governance.record_governance_event", side_effect=AuditWriteError("forced")):
            with self.assertRaises(GovernanceApprovalError):
                self.decide(audit_failure, self.ias)
        audit_failure.refresh_from_db()
        audit_failure.approval_case.refresh_from_db()
        self.assertEqual(audit_failure.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertEqual(audit_failure.approval_case.state, "PENDING")
        self.assertFalse(GovernanceApprovalDecision.objects.filter(case=audit_failure.approval_case).exists())

    def test_ias_self_confirmation_both_policy_modes_and_explicit_second_step(self):
        with override_settings(DOCUMENT_IAS_SELF_CONFIRM_ALLOWED=True):
            allowed = self.upload("IAS self confirm TEST/2026/22", user=self.ias)
            self.assertEqual(allowed.state, SubmissionState.PENDING_IAS_APPROVAL)
            self.assertFalse(DocumentIngestionJob.objects.filter(submission=allowed).exists())
            self.decide(allowed, self.ias)
            allowed.refresh_from_db()
            self.assertEqual(allowed.state, SubmissionState.QUEUED_FOR_INGESTION)
        with override_settings(DOCUMENT_IAS_SELF_CONFIRM_ALLOWED=False):
            denied = self.upload("IAS self confirm denied TEST/2026/23", user=self.ias)
            with self.assertRaisesMessage(GovernanceApprovalError, "self-approval"):
                self.decide(denied, self.ias)
            self.assertFalse(DocumentIngestionJob.objects.filter(submission=denied).exists())


class DocumentIngestionUnitTests(DocumentIntakeBase):
    def queued_job(self, text="Worker ingestion TEST/2026/31"):
        submission = self.upload(text)
        decide_approval_case(
            self.ias,
            submission.approval_case_id,
            decision=ApprovalDecisionType.APPROVE,
            reason="Approved for worker test.",
            expected_case_version=1,
            expected_subject_version=1,
            correlation_id=submission.correlation_id,
        )
        submission.refresh_from_db()
        return submission, submission.ingestion_job

    def test_successful_worker_is_durable_hash_checked_and_idempotent(self):
        submission, job = self.queued_job()
        claimed = claim_ingestion_job(job_id=job.pk, worker_id="test-worker")
        self.assertIsNotNone(claimed)
        ingestor = Mock()
        ingestor.process_entry.return_value = SimpleNamespace(
            status="SUCCESS", document_id=44, error_stage="", error_message=""
        )
        graph = Mock()
        graph.run_targeted.return_value = {
            "run_id": "kg-targeted",
            "new_candidates": 2,
            "existing_newly_resolved": 1,
            "reference_edges_inserted": 1,
            "issued_by_edges_inserted": 1,
        }
        completed = process_ingestion_job(claimed, ingestor=ingestor, graph_pipeline=graph)
        self.assertEqual(completed.state, JobState.SUCCEEDED)
        submission.refresh_from_db()
        self.assertEqual(submission.state, SubmissionState.INGESTED)
        self.assertEqual(submission.ingested_document_id, 44)
        archive = Path(settings.DOCUMENT_APPROVED_ROOT) / submission.archive_key
        self.assertTrue(archive.is_file())
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), submission.sha256)
        repeated = process_ingestion_job(completed, ingestor=Mock(), graph_pipeline=Mock())
        self.assertEqual(repeated.state, JobState.SUCCEEDED)
        self.assertEqual(ingestor.process_entry.call_count, 1)
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_INGESTION_STARTED,
            correlation_id=submission.correlation_id,
        ).exists())
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_INGESTION_SUCCEEDED,
            correlation_id=submission.correlation_id,
        ).exists())

    def test_hash_mismatch_missing_file_and_retry_controls(self):
        mismatch, mismatch_job = self.queued_job("Hash mismatch TEST/2026/32")
        quarantine_path(mismatch).write_bytes(b"%PDF-tampered")
        failed = process_ingestion_job(claim_ingestion_job(job_id=mismatch_job.pk), ingestor=Mock(), graph_pipeline=Mock())
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.last_error_code, "BYTE_INTEGRITY_MISMATCH")

        missing, missing_job = self.queued_job("Missing quarantine TEST/2026/33")
        quarantine_path(missing).unlink()
        failed_missing = process_ingestion_job(claim_ingestion_job(job_id=missing_job.pk), ingestor=Mock(), graph_pipeline=Mock())
        self.assertEqual(failed_missing.last_error_code, "QUARANTINE_FILE_MISSING")

        retryable, retry_job = self.queued_job("Retryable embedding TEST/2026/34")
        bad_ingestor = Mock()
        bad_ingestor.process_entry.return_value = SimpleNamespace(
            status="FAILED_EMBEDDING", document_id=0, error_stage="embedding", error_message="temporary model failure"
        )
        retry_state = process_ingestion_job(claim_ingestion_job(job_id=retry_job.pk), ingestor=bad_ingestor, graph_pipeline=Mock())
        self.assertEqual(retry_state.state, JobState.RETRYABLE)
        retryable.refresh_from_db()
        retried = retry_ingestion(actor=self.ias, submission_id=retryable.pk, expected_version=retryable.version)
        self.assertEqual(retried.state, JobState.PENDING)
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_INGESTION_RETRY_REQUESTED,
            correlation_id=retryable.correlation_id,
        ).exists())
        retryable.refresh_from_db()
        self.assertEqual(retryable.state, SubmissionState.QUEUED_FOR_INGESTION)
        with self.assertRaises(Exception):
            retry_ingestion(actor=self.officer, submission_id=retryable.pk, expected_version=retryable.version)

        reclaimed = claim_ingestion_job(job_id=retry_job.pk, worker_id="retry-worker")
        good_ingestor = Mock()
        good_ingestor.process_entry.return_value = SimpleNamespace(
            status="SUCCESS", document_id=45, error_stage="", error_message=""
        )
        good_graph = Mock()
        good_graph.run_targeted.return_value = {"new_candidates": 0, "existing_newly_resolved": 0}
        succeeded = process_ingestion_job(reclaimed, ingestor=good_ingestor, graph_pipeline=good_graph)
        self.assertEqual(succeeded.state, JobState.SUCCEEDED)
        self.assertEqual(succeeded.attempt_count, 2)

    def test_worker_rechecks_latest_validation_and_final_approval(self):
        validation_submission, validation_job = self.queued_job("Validation gate TEST/2026/36")
        claimed = claim_ingestion_job(job_id=validation_job.pk, worker_id="gate-worker")
        validation_submission.refresh_from_db()
        latest = validation_submission.validation_attempts.order_by("-attempt_number").first()
        DocumentValidationAttempt.objects.create(
            submission=validation_submission,
            attempt_number=latest.attempt_number + 1,
            requested_by=self.ias,
            validator_version="stage3d-test",
            result_code="TEST_FAILURE",
            validation_state=ValidationState.FAILED,
            security_status=SecurityStatus.FAILED,
            malware_status=validation_submission.malware_status,
            page_count=validation_submission.page_count,
            signature_status=validation_submission.signature_status,
        )
        validation_submission.validation_state = ValidationState.FAILED
        validation_submission.security_status = SecurityStatus.FAILED
        validation_submission.save(update_fields=("validation_state", "security_status", "updated_at"))
        failed = process_ingestion_job(claimed, ingestor=Mock(), graph_pipeline=Mock())
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.last_error_code, "VALIDATION_GATE_FAILED")

        approval_submission, approval_job = self.queued_job("Approval gate TEST/2026/37")
        claimed = claim_ingestion_job(job_id=approval_job.pk, worker_id="gate-worker")
        approval_submission.approval_case.state = "REJECTED"
        approval_submission.approval_case.save(update_fields=("state", "updated_at"))
        failed = process_ingestion_job(claimed, ingestor=Mock(), graph_pipeline=Mock())
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.last_error_code, "APPROVAL_GATE_FAILED")

    def test_approval_does_not_execute_heavy_pipeline(self):
        submission = self.upload("No request ingestion TEST/2026/35")
        with (
            patch("apps.document_intake.ingestion.BatchIngestor") as ingestor_class,
            patch("apps.document_intake.ingestion.KnowledgeGraphPipeline") as graph_class,
        ):
            decide_approval_case(
                self.ias,
                submission.approval_case_id,
                decision=ApprovalDecisionType.APPROVE,
                reason="Approved without synchronous ingestion.",
                expected_case_version=1,
                expected_subject_version=1,
                correlation_id=submission.correlation_id,
            )
        ingestor_class.assert_not_called()
        graph_class.assert_not_called()
        self.assertEqual(DocumentIngestionJob.objects.filter(submission=submission).count(), 1)


class DocumentIntakeUiTests(DocumentIntakeBase):
    def approved_submission(self, text="UI ingestion TEST/2026/60"):
        submission = self.upload(text)
        decide_approval_case(
            self.ias,
            submission.approval_case_id,
            decision=ApprovalDecisionType.APPROVE,
            reason="Approved for UI state test.",
            expected_case_version=1,
            expected_subject_version=1,
            correlation_id=submission.correlation_id,
        )
        submission.refresh_from_db()
        return submission

    def test_shared_shell_empty_state_and_human_audit_labels(self):
        self.client.force_login(self.ias)
        empty = self.client.get(reverse("governance:approval_case_list"))
        self.assertContains(empty, "No approval cases")
        self.assertNotContains(empty, "Unknown workflows")
        shell = empty.content.decode()
        self.assertIn('class="sidebar-navigation"', shell)
        sidebar = shell.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
        header = shell.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
        self.assertNotIn('account-menu', sidebar)
        self.assertIn('data-account-menu-toggle', header)
        self.assertIn('IAS Officer', header)
        self.assertIn('title="Submit document"', shell)
        self.assertIn('title="Audit log"', shell)

        submission = self.upload("UI presentation TEST/2026/51")
        audit = self.client.get(reverse("governance:audit_list"))
        self.assertContains(audit, "Document submitted")
        self.assertContains(audit, self.officer.username)
        self.assertContains(audit, "Document submission")
        self.assertNotContains(audit, "document_intake.DocumentSubmission")
        approval = self.client.get(reverse("governance:approval_case_list"))
        self.assertContains(approval, "Document corpus admission")
        self.assertContains(approval, "UI presentation TEST/2026/51")
        self.assertContains(approval, "Review")

    def test_submission_status_and_ias_detail_are_scoped(self):
        submission = self.upload("IAS detail TEST/2026/52")
        self.client.force_login(self.wrong_ias)
        self.assertEqual(self.client.get(reverse("document_intake:detail", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.ias)
        response = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(response, "IAS corpus admission decision")
        self.assertContains(response, "Bounded pre-approval preview")
        self.assertContains(response, "Malware scan unavailable in this local deployment")
        self.assertContains(response, "Approval")

    def test_ingestion_states_are_plain_language_and_retry_is_ias_only(self):
        submission = self.approved_submission()
        self.client.force_login(self.officer)
        pending = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(pending, "Waiting to be processed")
        self.assertContains(pending, "0 of 3")
        self.assertNotContains(pending, "Worker:")
        self.assertNotContains(pending, "Queue retry")

        job = submission.ingestion_job
        job.state = JobState.RUNNING
        job.current_stage = "PROCESSING_DOCUMENT"
        job.attempt_count = 1
        job.save(update_fields=("state", "current_stage", "attempt_count", "updated_at"))
        submission.state = SubmissionState.INGESTING
        submission.save(update_fields=("state", "updated_at"))
        running = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(running, "Processing document")
        self.assertContains(running, "Extracting pages and generating embeddings")

        job.state = JobState.RETRYABLE
        job.current_stage = "FAILED"
        job.safe_error_reason = "A temporary model problem interrupted processing."
        job.save(update_fields=("state", "current_stage", "safe_error_reason", "updated_at"))
        submission.state = SubmissionState.INGESTION_FAILED
        submission.save(update_fields=("state", "updated_at"))
        officer_retryable = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(officer_retryable, "Waiting for another attempt")
        self.assertNotContains(officer_retryable, "Queue retry")

        self.client.force_login(self.ias)
        ias_retryable = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(ias_retryable, "Queue retry")
        self.assertContains(ias_retryable, "Job ID")

        job.state = JobState.SUCCEEDED
        job.current_stage = "COMPLETED"
        job.finished_at = timezone.now()
        job.kg_result = {"new_candidates": 2, "existing_newly_resolved": 1, "pipeline": {"pages": 6, "chunks": 8, "embeddings": 8}}
        job.save(update_fields=("state", "current_stage", "finished_at", "kg_result", "updated_at"))
        submission.state = SubmissionState.INGESTED
        submission.ingested_document_id = 88
        submission.save(update_fields=("state", "ingested_document_id", "updated_at"))
        succeeded = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(succeeded, "Document admitted to corpus")
        self.assertContains(succeeded, "KG candidates generated")
