from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from knowledge_engine.chunking.chunker import Chunk
from knowledge_engine.extraction.pdf import ExtractedDocument, ExtractedPage
from knowledge_engine.metadata.validation import validate_metadata
from knowledge_engine.pipelines.batch_ingest import (
    BatchIngestor,
    BatchResult,
    ManifestEntry,
    ManifestValidationError,
    Status,
    execute_manifest,
    parse_manifest,
)
from knowledge_engine.settings import settings


class FakeStorage:
    def __init__(self, identity=None, fail_write: bool = False):
        self.identity = identity
        self.fail_write = fail_write
        self.identity_calls = []
        self.write_calls = []

    def find_document_identity(self, file_path, sha256):
        self.identity_calls.append((file_path, sha256))
        return self.identity

    def replace_document(self, **kwargs):
        self.write_calls.append(kwargs)
        if self.fail_write:
            raise RuntimeError("simulated transaction rollback")
        return 7

    def counts(self):
        return {"documents": 0, "pages": 0, "chunks": 0}


class FakeChunker:
    def __init__(self, chunks=None):
        self.chunks = chunks or [Chunk(0, 1, 1, 3, "मराठी पुरावा")]
        self.calls = 0

    def chunk_pages(self, pages):
        self.calls += 1
        return self.chunks


class FakeEmbedder:
    def __init__(self, dimension=settings.embed_dim, count=None):
        self.dimension = dimension
        self.count = count
        self.calls = 0

    def encode(self, texts, show_progress_bar=False):
        self.calls += 1
        count = len(texts) if self.count is None else self.count
        return [[0.0] * self.dimension for _ in range(count)]


def fake_extracted():
    return ExtractedDocument(
        [ExtractedPage(1, "महाराष्ट्र शासन विषय चाचणी प्रशासन निर्णय", "digital", 1.0)]
    )


def fake_metadata(text, department, file_name):
    return {
        "department": department,
        "document_type": "शासन निर्णय",
        "gr_number": "मावअ-2024/प्र.क्र.50",
        "gr_date": None,
        "gr_date_source": None,
        "subject": "प्रशासकीय कामांना मान्यता देण्याबाबत शासन निर्णय",
        "references_text": None,
    }


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "GRs"
        self.root.mkdir()

    def pdf(self, relative="Finance Department/doc.pdf", content=b"pdf"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def manifest(self, rows, headings=None):
        path = Path(self.temporary.name) / "manifest.csv"
        fieldnames = headings or ["file_path", "department", "document_label", "notes"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def entry(self, content=b"pdf"):
        path = self.pdf(content=content)
        return ManifestEntry(
            2, "Finance Department/doc.pdf", "Finance Department", "label", "notes", path
        )

    def ingestor(self, storage=None, chunker=None, embedder=None, **kwargs):
        return BatchIngestor(
            storage=storage or FakeStorage(),
            chunker=chunker or FakeChunker(),
            embedder=embedder or FakeEmbedder(),
            extract_pdf_fn=kwargs.pop("extract_pdf_fn", lambda path: fake_extracted()),
            extract_metadata_fn=fake_metadata,
            validate_metadata_fn=validate_metadata,
            **kwargs,
        )


class ManifestParsingTests(BatchTestCase):
    def test_manifest_parses_relative_safe_pdf_and_optional_fields(self):
        self.pdf()
        path = self.manifest([{
            "file_path": "Finance Department/doc.pdf",
            "department": "Finance Department",
            "document_label": "finance-one",
            "notes": "controlled",
        }])
        entry = parse_manifest(path, self.root)[0]
        self.assertEqual(entry.file_path, "Finance Department/doc.pdf")
        self.assertEqual(entry.document_label, "finance-one")

    def test_path_traversal_is_rejected(self):
        outside = self.root.parent / "outside.pdf"
        outside.write_bytes(b"pdf")
        path = self.manifest([{"file_path": "../outside.pdf", "department": "X"}])
        with self.assertRaisesRegex(ManifestValidationError, "outside GR_ROOT"):
            parse_manifest(path, self.root)

    def test_missing_pdf_and_non_pdf_are_rejected(self):
        self.pdf("Finance Department/not-pdf.txt")
        path = self.manifest([
            {"file_path": "missing.pdf", "department": "X"},
            {"file_path": "Finance Department/not-pdf.txt", "department": "X"},
        ])
        with self.assertRaises(ManifestValidationError) as context:
            parse_manifest(path, self.root)
        message = str(context.exception)
        self.assertIn("does not exist", message)
        self.assertIn("not a PDF", message)

    def test_duplicate_manifest_rows_are_rejected(self):
        self.pdf()
        row = {"file_path": "Finance Department/doc.pdf", "department": "Finance Department"}
        path = self.manifest([row, row])
        with self.assertRaisesRegex(ManifestValidationError, "duplicate"):
            parse_manifest(path, self.root)


class BatchPipelineTests(BatchTestCase):
    def test_sha_identity_and_unchanged_document_skip(self):
        entry = self.entry(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        storage = FakeStorage({"id": 4, "file_path": entry.database_path, "sha256": digest})
        embedder = FakeEmbedder()
        result = self.ingestor(storage=storage, embedder=embedder).process_entry(entry, run_id="test")
        self.assertEqual(result.sha256, digest)
        self.assertEqual(result.status, Status.SKIPPED_UNCHANGED.value)
        self.assertEqual(storage.write_calls, [])
        self.assertEqual(embedder.calls, 0)

    def test_existing_document_is_reprocessed_with_force(self):
        entry = self.entry(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        storage = FakeStorage({"id": 4, "file_path": entry.database_path, "sha256": digest})
        result = self.ingestor(storage=storage).process_entry(entry, run_id="test", force=True)
        self.assertEqual(result.operation, "REPROCESSED")
        self.assertEqual(result.status, Status.SUCCESS_WITH_WARNING.value)
        self.assertEqual(len(storage.write_calls), 1)

    def test_changed_existing_document_is_reprocessed_without_force(self):
        entry = self.entry(b"changed")
        storage = FakeStorage({"id": 4, "file_path": entry.database_path, "sha256": "old"})
        result = self.ingestor(storage=storage).process_entry(entry, run_id="test")
        self.assertEqual(result.operation, "REPROCESSED")
        self.assertEqual(len(storage.write_calls), 1)

    def test_chunk_invariant_prevents_database_write(self):
        entry = self.entry()
        chunks = [Chunk(0, 1, 1, settings.chunk_max_tokens + 1, "too large")]
        storage = FakeStorage()
        result = self.ingestor(storage=storage, chunker=FakeChunker(chunks)).process_entry(entry, run_id="test")
        self.assertEqual(result.status, Status.FAILED_CHUNKING.value)
        self.assertEqual(storage.write_calls, [])

    def test_embedding_dimension_and_count_are_enforced(self):
        entry = self.entry()
        for embedder in (FakeEmbedder(dimension=3), FakeEmbedder(count=0)):
            with self.subTest(embedder=embedder.__dict__):
                storage = FakeStorage()
                result = self.ingestor(storage=storage, embedder=embedder).process_entry(entry, run_id="test")
                self.assertEqual(result.status, Status.FAILED_EMBEDDING.value)
                self.assertEqual(storage.write_calls, [])

    def test_database_failure_is_isolated_as_rollback_status(self):
        entry = self.entry()
        storage = FakeStorage(fail_write=True)
        result = self.ingestor(storage=storage).process_entry(entry, run_id="test")
        self.assertEqual(result.status, Status.FAILED_DATABASE.value)
        self.assertEqual(result.error_stage, "database")
        self.assertIn("rollback", result.error_message)

    def test_dry_run_has_zero_extraction_embedding_and_database_writes(self):
        entry = self.entry()
        storage = FakeStorage()
        embedder = FakeEmbedder()
        extraction_calls = []
        result = self.ingestor(
            storage=storage,
            embedder=embedder,
            extract_pdf_fn=lambda path: extraction_calls.append(path),
        ).process_entry(entry, run_id="test", dry_run=True)
        self.assertEqual(result.status, Status.DRY_RUN.value)
        self.assertEqual(extraction_calls, [])
        self.assertEqual(embedder.calls, 0)
        self.assertEqual(storage.write_calls, [])

    def test_preflight_rejects_missing_shared_ocr_dependency(self):
        ingestor = self.ingestor()
        entry = self.entry()
        from unittest.mock import patch
        with patch(
            "knowledge_engine.pipelines.batch_ingest._tesseract_available",
            return_value=False,
        ), self.assertRaisesRegex(RuntimeError, "Tesseract is unavailable"):
            ingestor.preflight([entry], force=True)

    def test_preflight_does_not_require_ocr_when_every_document_will_skip(self):
        entry = self.entry(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        storage = FakeStorage({"id": 4, "file_path": entry.database_path, "sha256": digest})
        ingestor = self.ingestor(storage=storage)
        from unittest.mock import patch
        with patch(
            "knowledge_engine.pipelines.batch_ingest._tesseract_available",
            return_value=False,
        ):
            ingestor.preflight([entry], force=False)

    def test_preflight_loads_embedding_provider_once_for_the_batch(self):
        entry = self.entry()
        instances = []

        def factory():
            embedder = FakeEmbedder()
            instances.append(embedder)
            return embedder

        ingestor = BatchIngestor(
            storage=FakeStorage(),
            chunker=FakeChunker(),
            embedder_factory=factory,
            extract_pdf_fn=lambda path: fake_extracted(),
            extract_metadata_fn=fake_metadata,
            validate_metadata_fn=validate_metadata,
        )
        from unittest.mock import patch
        with patch(
            "knowledge_engine.pipelines.batch_ingest._tesseract_available",
            return_value=True,
        ):
            ingestor.preflight([entry], force=True)
        self.assertIs(ingestor.embedder, instances[0])
        self.assertEqual(len(instances), 1)


class BatchRunStateTests(BatchTestCase):
    def test_one_failure_does_not_stop_later_rows_and_outputs_are_structured(self):
        rows = []
        for index in range(3):
            relative = f"Department/doc-{index}.pdf"
            self.pdf(relative, bytes([index]))
            rows.append({"file_path": relative, "department": "Department"})
        manifest = self.manifest(rows)

        class IsolatedIngestor:
            def process_entry(self, entry, **kwargs):
                status = Status.FAILED_EXTRACTION.value if entry.file_name == "doc-1.pdf" else Status.SUCCESS.value
                return BatchResult(
                    run_id=kwargs["run_id"], file_path=entry.file_path,
                    file_name=entry.file_name, department=entry.department,
                    sha256=hashlib.sha256(entry.absolute_path.read_bytes()).hexdigest(),
                    operation="NEW", status=status,
                    error_stage="extraction" if "FAILED" in status else "",
                    error_message="bad PDF" if "FAILED" in status else "",
                )

        output_root = Path(self.temporary.name) / "outputs"
        run_dir, results, summary = execute_manifest(
            manifest, gr_root=self.root, ingestor=IsolatedIngestor(),
            output_root=output_root, run_id="isolation-test",
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(results[2].status, Status.SUCCESS.value)
        self.assertEqual(summary["failure_count"], 1)
        self.assertTrue((run_dir / "manifest_snapshot.csv").is_file())
        self.assertTrue((run_dir / "results.csv").is_file())
        self.assertTrue((run_dir / "summary.json").is_file())
        self.assertTrue((run_dir / "errors.csv").is_file())
        stored = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["document_count"], 3)

    def test_resume_does_not_reprocess_completed_unchanged_rows(self):
        self.pdf(content=b"same")
        manifest = self.manifest([{
            "file_path": "Finance Department/doc.pdf",
            "department": "Finance Department",
        }])
        output_root = Path(self.temporary.name) / "outputs"

        class CountingIngestor:
            def __init__(self): self.calls = 0
            def process_entry(self, entry, **kwargs):
                self.calls += 1
                return BatchResult(
                    run_id=kwargs["run_id"], file_path=entry.file_path,
                    file_name=entry.file_name, department=entry.department,
                    sha256=hashlib.sha256(entry.absolute_path.read_bytes()).hexdigest(),
                    operation="NEW", status=Status.SUCCESS.value,
                )

        first = CountingIngestor()
        execute_manifest(
            manifest, gr_root=self.root, ingestor=first,
            output_root=output_root, run_id="resume-test",
        )
        second = CountingIngestor()
        _run_dir, results, _summary = execute_manifest(
            manifest, gr_root=self.root, ingestor=second,
            output_root=output_root, run_id="resume-test", resume=True,
        )
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)
        self.assertEqual(results[0].status, Status.SUCCESS.value)
