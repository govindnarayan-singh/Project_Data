from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import numpy as np
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase, override_settings
from psycopg.conninfo import make_conninfo
from reportlab.pdfgen import canvas

from apps.document_intake.ingestion import claim_ingestion_job, process_ingestion_job
from apps.document_intake.models import JobState, SubmissionState
from apps.document_intake.services import submit_document
from apps.governance.audit import verify_audit_chain
from apps.governance.constants import GENESIS_HASH, ApprovalDecisionType, AuditAction
from apps.governance.models import GovernanceAuditChain, GovernanceAuditEvent
from apps.governance.services import decide_approval_case
from apps.identity_access.models import AccessGrant, AccessRole, IASAuthorityScope, OrganizationUnit
from knowledge_engine.chunking.chunker import TokenChunker
from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline
from knowledge_engine.graph.quality import QualityPolicy
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.pipelines.batch_ingest import BatchIngestor, ManifestEntry
from knowledge_engine.settings import settings
from knowledge_engine.storage.postgres import PostgresStorage


class DeterministicEmbedder:
    def encode(self, texts, show_progress_bar=False):
        return np.asarray([[0.001] * settings.embed_dim for _ in texts], dtype=np.float32)


class WhitespaceTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()


def make_test_chunker():
    chunker = object.__new__(TokenChunker)
    chunker.max_tokens = settings.chunk_max_tokens
    chunker.overlap_tokens = settings.chunk_overlap_tokens
    chunker.tokenizer = WhitespaceTokenizer()
    return chunker


def django_test_dsn() -> str:
    config = connection.settings_dict
    values = {
        "dbname": config["NAME"],
        "user": config.get("USER"),
        "password": config.get("PASSWORD"),
        "host": config.get("HOST"),
        "port": config.get("PORT"),
    }
    return make_conninfo(**{key: value for key, value in values.items() if value not in (None, "")})


class Stage3PipelinePostgreSQLTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise AssertionError("Stage 3 pipeline integration must run against PostgreSQL.")
        if os.getenv("RUN_STAGE3_PIPELINE_INTEGRATION") != "1":
            raise AssertionError("Set RUN_STAGE3_PIPELINE_INTEGRATION=1 for isolated Stage 3 pipeline validation.")

    def test_small_pdf_uses_existing_pipeline_and_is_idempotent(self):
        dsn = django_test_dsn()
        storage = PostgresStorage(dsn)
        storage.ensure_schema()
        graph_repository = GraphRepository(dsn)
        migrations = Path(django_settings.BASE_DIR) / "deployment" / "postgres" / "migrations"
        with graph_repository.connect() as db:
            for migration in sorted(migrations.glob("*.sql")):
                db.execute(migration.read_text(encoding="utf-8"))
            db.commit()
        with storage.connect() as db:
            before = db.execute(
                """SELECT
                     (SELECT count(*) FROM documents) documents,
                     (SELECT count(*) FROM pages) pages,
                     (SELECT count(*) FROM chunks) chunks,
                     (SELECT count(*) FROM kg_reference_candidates) candidates,
                     (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by"""
            ).fetchone()

        tmp_root = Path(django_settings.BASE_DIR) / "tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="stage3-pipeline-") as directory:
            pdf_path = Path(directory) / "approved.pdf"
            pdf = canvas.Canvas(str(pdf_path), pagesize=(420, 540))
            pdf.drawString(30, 500, "Government Resolution Number NEW/2026/91")
            pdf.drawString(30, 475, "Government of Maharashtra")
            pdf.drawString(30, 430, "References")
            pdf.drawString(30, 405, "Government Resolution No. OLD/2020/1 dated 01/01/2020")
            pdf.showPage()
            pdf.save()

            entry = ManifestEntry(
                row_number=1,
                file_path=f"{uuid.uuid4()}/original.pdf",
                department="Stage 3 Integration Department",
                document_label="Stage 3 pipeline fixture",
                notes="Isolated test database only",
                absolute_path=pdf_path,
                source_origin="APPROVED_SUBMISSION",
                source_authority="OFFICER_PROVIDED_UNVERIFIED",
            )
            ingestor = BatchIngestor(
                storage=storage,
                chunker=make_test_chunker(),
                embedder=DeterministicEmbedder(),
            )
            first = ingestor.process_entry(entry, run_id="stage3-pipeline-first")
            self.assertIn(first.status, {"SUCCESS", "SUCCESS_WITH_WARNING"})
            self.assertEqual(first.pages_total, 1)
            self.assertGreater(first.chunks_created, 0)
            self.assertEqual(first.embedding_count, first.chunks_created)
            self.assertEqual(first.embedding_dimension, 1024)

            graph = KnowledgeGraphPipeline(repository=graph_repository, quality_policy=QualityPolicy([]))
            kg = graph.run_targeted(first.document_id)
            self.assertGreaterEqual(kg["new_candidates"], 1)
            self.assertEqual(kg["issued_by_edges_inserted"], 1)

            second = ingestor.process_entry(entry, run_id="stage3-pipeline-second")
            self.assertEqual(second.status, "SKIPPED_UNCHANGED")
            self.assertEqual(second.document_id, first.document_id)
            with storage.connect() as db:
                counts = db.execute(
                    """SELECT
                         (SELECT count(*) FROM documents) documents,
                         (SELECT count(*) FROM pages) pages,
                         (SELECT count(*) FROM chunks) chunks,
                         (SELECT count(*) FROM kg_reference_candidates) candidates,
                         (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by,
                         (SELECT max(vector_dims(embedding)) FROM chunks) dimensions"""
                ).fetchone()
            self.assertEqual(int(counts["documents"]), int(before["documents"]) + 1)
            self.assertEqual(int(counts["pages"]), int(before["pages"]) + 1)
            self.assertEqual(int(counts["chunks"]), int(before["chunks"]) + first.chunks_created)
            self.assertGreaterEqual(int(counts["candidates"]), int(before["candidates"]) + 1)
            self.assertEqual(int(counts["issued_by"]), int(before["issued_by"]) + 1)
            self.assertEqual(int(counts["dimensions"]), 1024)

    def test_approved_submission_runs_end_to_end_through_worker_path(self):
        dsn = django_test_dsn()
        storage = PostgresStorage(dsn)
        storage.ensure_schema()
        graph_repository = GraphRepository(dsn)
        migrations = Path(django_settings.BASE_DIR) / "deployment" / "postgres" / "migrations"
        with graph_repository.connect() as db:
            for migration in sorted(migrations.glob("*.sql")):
                db.execute(migration.read_text(encoding="utf-8"))
            db.commit()

        tmp_root = Path(django_settings.BASE_DIR) / "tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="stage3-worker-e2e-") as directory:
            root = Path(directory)
            pdf_path = root / "approved.pdf"
            pdf = canvas.Canvas(str(pdf_path), pagesize=(420, 540))
            pdf.drawString(30, 500, "Government Resolution Number E2E/2026/92")
            pdf.drawString(30, 475, "Government of Maharashtra")
            pdf.drawString(30, 430, "References")
            pdf.drawString(30, 405, "Government Resolution No. OLD/2020/1 dated 01/01/2020")
            pdf.showPage()
            pdf.save()

            GovernanceAuditChain.objects.get_or_create(pk=1, defaults={"last_sequence": 0, "head_hash": GENESIS_HASH})
            organization = OrganizationUnit.objects.create(code="stage3-e2e", name="Stage 3 E2E Department")
            users = get_user_model()
            officer = users.objects.create_user("stage3-e2e-officer", password="TestOnlyPass-9321")
            ias = users.objects.create_user("stage3-e2e-ias", password="TestOnlyPass-9321")
            AccessGrant.objects.create(user=officer, organization=organization, role=AccessRole.OFFICER)
            AccessGrant.objects.create(user=ias, organization=organization, role=AccessRole.IAS_OFFICER)
            IASAuthorityScope.objects.create(user=ias, organization=organization)

            with override_settings(
                DOCUMENT_QUARANTINE_ROOT=root / "Incoming" / "Officer_Uploads",
                DOCUMENT_APPROVED_ROOT=root / "Approved_Submissions",
                DOCUMENT_MALWARE_SCANNER="",
                DOCUMENT_MALWARE_SCANNER_EXECUTABLE="",
            ):
                submission = submit_document(
                    actor=officer,
                    upload=SimpleUploadedFile("approved.pdf", pdf_path.read_bytes(), content_type="application/pdf"),
                    organization=organization,
                    claimed_identifier="E2E/2026/92",
                    claimed_source_url="https://example.gov/e2e.pdf",
                    submission_reason="Isolated Stage 3D production-worker integration test.",
                )
                self.assertEqual(submission.state, SubmissionState.PENDING_IAS_APPROVAL)
                decide_approval_case(
                    ias,
                    submission.approval_case_id,
                    decision=ApprovalDecisionType.APPROVE,
                    reason="Approve isolated worker integration fixture.",
                    expected_case_version=1,
                    expected_subject_version=1,
                    correlation_id=submission.correlation_id,
                )
                submission.refresh_from_db()
                claimed = claim_ingestion_job(job_id=submission.ingestion_job.pk, worker_id="stage3-e2e-worker")
                ingestor = BatchIngestor(storage=storage, chunker=make_test_chunker(), embedder=DeterministicEmbedder())
                graph = KnowledgeGraphPipeline(repository=graph_repository, quality_policy=QualityPolicy([]))
                completed = process_ingestion_job(claimed, ingestor=ingestor, graph_pipeline=graph)

            completed.refresh_from_db()
            submission.refresh_from_db()
            self.assertEqual(completed.state, JobState.SUCCEEDED)
            self.assertEqual(submission.state, SubmissionState.INGESTED)
            self.assertGreater(submission.ingested_document_id, 0)
            self.assertGreater(completed.kg_result["pipeline"]["pages"], 0)
            self.assertGreater(completed.kg_result["pipeline"]["chunks"], 0)
            self.assertEqual(completed.kg_result["pipeline"]["chunks"], completed.kg_result["pipeline"]["embeddings"])
            self.assertEqual(completed.kg_result["pipeline"]["embedding_dimension"], 1024)
            self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.DOCUMENT_INGESTION_STARTED).exists())
            self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.DOCUMENT_INGESTION_SUCCEEDED).exists())
            self.assertTrue(verify_audit_chain().valid)
