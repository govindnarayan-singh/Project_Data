from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.document_intake.ingestion import claim_ingestion_job, retry_ingestion
from apps.document_intake.models import (
    DocumentIngestionJob,
    DocumentSubmission,
    DuplicateStatus,
    JobState,
    MalwareStatus,
    SecurityStatus,
    SourceProvenance,
    SubmissionState,
    ValidationState,
)
from apps.governance.constants import GENESIS_HASH, ApprovalCaseState, ApprovalDecisionType, AuditAction
from apps.governance.models import (
    GovernanceApprovalCase,
    GovernanceApprovalDecision,
    GovernanceAuditChain,
    GovernanceAuditEvent,
)
from apps.identity_access.models import AccessGrant, AccessRole, IASAuthorityScope, OrganizationUnit


class DocumentIngestionPostgreSQLTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise AssertionError("Stage 3 job-locking tests must run against PostgreSQL.")

    def setUp(self):
        GovernanceAuditChain.objects.get_or_create(pk=1, defaults={"last_sequence": 0, "head_hash": GENESIS_HASH})
        self.organization = OrganizationUnit.objects.create(code=f"job-{uuid.uuid4().hex[:8]}", name="Job Scope")
        users = get_user_model()
        self.officer = users.objects.create_user(f"officer-{uuid.uuid4().hex[:8]}", password="TestOnlyPass-9321")
        self.ias = users.objects.create_user(f"ias-{uuid.uuid4().hex[:8]}", password="TestOnlyPass-9321")
        AccessGrant.objects.create(user=self.officer, organization=self.organization, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.ias, organization=self.organization, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.organization)

    def make_job(self, *, state=JobState.PENDING, submission_state=SubmissionState.QUEUED_FOR_INGESTION,
                 attempts=0, max_attempts=3, lease_expires_at=None):
        key = uuid.uuid4().hex
        submission = DocumentSubmission.objects.create(
            organization=self.organization,
            submitted_by=self.officer,
            submitter_role_snapshot=AccessRole.OFFICER,
            original_filename="job.pdf",
            storage_key=f"{key}/original.pdf",
            sha256=key + key,
            byte_size=100,
            declared_mime="application/pdf",
            state=submission_state,
            validation_state=ValidationState.PASSED,
            security_status=SecurityStatus.PASSED,
            malware_status=MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED,
            source_provenance=SourceProvenance.OFFICER_PROVIDED_UNVERIFIED,
            submission_reason="PostgreSQL job test",
            duplicate_status=DuplicateStatus.NEW,
        )
        case = GovernanceApprovalCase.objects.create(
            workflow_type="DOCUMENT_CORPUS_ADMISSION",
            organization=self.organization,
            submitted_by=self.officer,
            subject_type="document_intake.DocumentSubmission",
            subject_id=str(submission.pk),
            subject_version=1,
            subject_digest=submission.sha256,
            state=ApprovalCaseState.APPROVED,
            version=2,
        )
        decision = GovernanceApprovalDecision(
            case=case,
            actor=self.ias,
            actor_role_snapshot=AccessRole.IAS_OFFICER,
            organization_id_snapshot=self.organization.pk,
            decision=ApprovalDecisionType.APPROVE,
            reason="PostgreSQL worker fixture approval.",
            subject_version_seen=1,
            case_version_seen=1,
        )
        decision._governance_insert_allowed = True
        decision.save(force_insert=True)
        submission.approval_case = case
        submission.save(update_fields=("approval_case", "updated_at"))
        job = DocumentIngestionJob.objects.create(
            submission=submission,
            state=state,
            attempt_count=attempts,
            max_attempts=max_attempts,
            idempotency_key=f"test:{key}",
            lease_expires_at=lease_expires_at,
        )
        return submission, job

    def test_two_workers_claim_one_job_once(self):
        submission, job = self.make_job()

        def claim(worker):
            close_old_connections()
            try:
                result = claim_ingestion_job(job_id=job.pk, worker_id=worker)
                return str(result.pk) if result else None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("worker-a", "worker-b")))
        self.assertEqual(sum(result is not None for result in results), 1)
        job.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(submission.state, SubmissionState.INGESTING)
        self.assertIsNone(claim_ingestion_job(job_id=job.pk, worker_id="worker-c"))

    def test_expired_lease_recovery_retry_and_attempt_bound(self):
        submission, job = self.make_job(
            state=JobState.RUNNING,
            submission_state=SubmissionState.INGESTING,
            attempts=1,
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )
        claimed = claim_ingestion_job(job_id=job.pk, worker_id="recovery-worker")
        job.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual(claimed.pk, job.pk)
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(submission.state, SubmissionState.INGESTING)
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_INGESTION_FAILED,
            resource_id=str(job.pk),
        ).exists())

        exhausted_submission, exhausted = self.make_job(
            state=JobState.RUNNING,
            submission_state=SubmissionState.INGESTING,
            attempts=3,
            max_attempts=3,
            lease_expires_at=timezone.now() - timedelta(minutes=5),
        )
        claim_ingestion_job(job_id=exhausted.pk, worker_id="recovery-worker")
        exhausted.refresh_from_db()
        exhausted_submission.refresh_from_db()
        self.assertEqual(exhausted.state, JobState.FAILED)
        self.assertEqual(exhausted_submission.state, SubmissionState.INGESTION_FAILED)

    def test_workers_claim_distinct_jobs_without_starvation(self):
        first_submission, first = self.make_job()
        second_submission, second = self.make_job()
        claimed_first = claim_ingestion_job(worker_id="worker-a")
        claimed_second = claim_ingestion_job(worker_id="worker-b")
        self.assertEqual({claimed_first.pk, claimed_second.pk}, {first.pk, second.pk})
        first_submission.refresh_from_db()
        second_submission.refresh_from_db()
        self.assertEqual(first_submission.state, SubmissionState.INGESTING)
        self.assertEqual(second_submission.state, SubmissionState.INGESTING)

    def test_claim_filters_invalid_approval_rejection_and_validation(self):
        pending_submission, pending = self.make_job(submission_state=SubmissionState.PENDING_IAS_APPROVAL)
        pending_submission.approval_case.state = ApprovalCaseState.PENDING
        pending_submission.approval_case.save(update_fields=("state", "updated_at"))
        self.assertIsNone(claim_ingestion_job(job_id=pending.pk, worker_id="gate-worker"))

        rejected_submission, rejected = self.make_job(submission_state=SubmissionState.REJECTED)
        self.assertIsNone(claim_ingestion_job(job_id=rejected.pk, worker_id="gate-worker"))

        invalid_submission, invalid = self.make_job()
        invalid_submission.validation_state = ValidationState.FAILED
        invalid_submission.save(update_fields=("validation_state", "updated_at"))
        self.assertIsNone(claim_ingestion_job(job_id=invalid.pk, worker_id="gate-worker"))
