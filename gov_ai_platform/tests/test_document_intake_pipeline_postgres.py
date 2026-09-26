from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import numpy as np
from django.conf import settings as django_settings
from django.db import connection
from django.test import TransactionTestCase
from psycopg.conninfo import make_conninfo
from reportlab.pdfgen import canvas

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
