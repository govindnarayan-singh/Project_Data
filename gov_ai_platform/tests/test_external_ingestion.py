from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from knowledge_engine.documents.sources import UnsafeDocumentPath, resolve_document_path
from knowledge_engine.external_ingestion.service import FROZEN_MANIFEST_SHA256
from knowledge_engine.graph.parser import ReferenceParser
from knowledge_engine.graph.quality import QualityPolicy
from knowledge_engine.graph.resolver import ReferenceResolver
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.pipelines.batch_ingest import BatchIngestor, ManifestEntry
from knowledge_engine.settings import PROJECT_ROOT
from tests.test_batch_ingest import FakeChunker, FakeEmbedder, fake_extracted, fake_metadata


class SourceOriginTests(SimpleTestCase):
    def test_multi_root_resolution_and_traversal_rejection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local"
            external = root / "external"
            local.mkdir()
            external.mkdir()
            (local / "local.pdf").write_bytes(b"%PDF-")
            (external / "official.pdf").write_bytes(b"%PDF-")
            fake_settings = SimpleNamespace(gr_root=local, external_document_root=external)
            with patch("knowledge_engine.documents.sources.settings", fake_settings):
                assert resolve_document_path("LOCAL_ORIGINAL", "local.pdf") == local / "local.pdf"
                assert resolve_document_path("EXTERNAL_OFFICIAL", "official.pdf") == external / "official.pdf"
                for unsafe in ("../escape.pdf", str((root / "absolute.pdf").resolve())):
                    with self.assertRaises(UnsafeDocumentPath):
                        resolve_document_path("EXTERNAL_OFFICIAL", unsafe)
                with self.assertRaises(UnsafeDocumentPath):
                    resolve_document_path("UNTRUSTED", "official.pdf")

    def test_frozen_manifest_sha_and_migration_contract(self):
        manifest = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v1.csv"
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == FROZEN_MANIFEST_SHA256
        migration = (PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "005_external_ingestion_v1.sql").read_text(encoding="utf-8")
        for contract in ("source_origin", "external_document_provenance", "external_acquisition_lifecycle_events", "uq_documents_origin_path"):
            assert contract in migration


class ExternalActIngestionTests(SimpleTestCase):
    def test_act_metadata_is_kind_aware_and_provenance_is_forwarded(self):
        class Storage:
            def __init__(self):
                self.payload = None

            def find_document_identity(self, file_path, sha256, source_origin):
                return None

            def replace_document(self, **kwargs):
                self.payload = kwargs
                return 99

        with TemporaryDirectory() as directory:
            pdf = Path(directory) / "act.pdf"
            pdf.write_bytes(b"%PDF-test")
            entry = ManifestEntry(
                2, "act.pdf", "India Code", "The Example Act, 1986", "ACT|EXAMPLE_ACT_1986", pdf,
                source_origin="EXTERNAL_OFFICIAL", source_authority="OFFICIAL_PRIMARY",
                external_catalogue_id=12, document_kind="ACT", canonical_identity="EXAMPLE_ACT_1986",
                document_year=1986, original_acquisition_path="data/acquisition/quarantine/act.pdf",
            )
            storage = Storage()
            result = BatchIngestor(
                storage=storage, chunker=FakeChunker(), embedder=FakeEmbedder(),
                extract_pdf_fn=lambda _path: fake_extracted(), extract_metadata_fn=fake_metadata,
            ).process_entry(entry, run_id="test")
            assert result.status == "SUCCESS"
            assert result.metadata_status == "VALID"
            assert result.document_id == 99
            assert storage.payload["source_origin"] == "EXTERNAL_OFFICIAL"
            assert storage.payload["external_catalogue_id"] == 12
            assert storage.payload["extracted_metadata"]["gr_number"] is None
            assert storage.payload["extracted_metadata"]["canonical_title"] == "The Example Act, 1986"


class ExternalGraphResolutionTests(SimpleTestCase):
    def test_act_canonical_identity_resolves_without_gr_number(self):
        quality = QualityPolicy([])
        source = {"id": 1, "file_name": "source.pdf", "department": "X", "gr_number": "SOURCE/1", "gr_date": None}
        target = {
            "id": 2, "file_name": "act.pdf", "department": "India Code", "gr_number": None,
            "gr_date": None, "document_kind": "ACT", "canonical_identity": "ENVIRONMENT_PROTECTION_ACT_1986",
        }
        candidate = ReferenceParser().parse_document(
            source, [{"page_number": 1, "text": "The Environment (Protection) Act, 1986"}]
        )[0]
        resolution = ReferenceResolver([source, target], quality, auto_verify_enabled=True).resolve(candidate)
        assert resolution.target_document_id == 2
        assert resolution.status.value == "RESOLVED_EXACT"

    def test_strong_relation_requires_external_provenance(self):
        candidate = {
            "id": 1, "source_document_id": 10, "resolved_target_document_id": 20,
            "candidate_relation_type": "AMENDS", "reference_kind": "GOVERNMENT_RESOLUTION",
            "raw_evidence_text": "Government Resolution No. ABC/2020/1 is hereby amended",
            "normalized_document_number": "abc/2020/1", "parser_status": "VALID",
            "resolution_status": "RESOLVED_EXACT", "blocked_by_page_quality": False,
            "blocked_by_target_metadata": False, "target_source_origin": "EXTERNAL_OFFICIAL",
            "target_external_candidate_id": None,
        }
        decision = KnowledgeGraphValidationPolicy("auto").strong_relation_decision(candidate)
        assert not decision.approved
        assert decision.reason == "External target provenance is incomplete"
