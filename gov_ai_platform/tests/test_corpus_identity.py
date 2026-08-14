from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase
from pypdf import PdfWriter

from knowledge_engine.acquisition.models import LocalPdf
from knowledge_engine.corpus_identity.extraction import (
    detect_document_kind,
    extract_multiline_document_number,
    identity_from_pages,
    probe_pdf_identity,
)
from knowledge_engine.corpus_identity.manifest import build_manifest, validate_manifest
from knowledge_engine.corpus_identity.models import CATALOG_VERSION, LocalResolution
from knowledge_engine.corpus_identity.repository import CorpusIdentityRepository
from knowledge_engine.corpus_identity.resolver import resolve_local_targets
from knowledge_engine.corpus_identity.service import catalogue_action
from knowledge_engine.graph.normalization import normalize_department_alias


def local_pdf(path: str = "Finance/target.pdf", digest: str = "a" * 64) -> LocalPdf:
    return LocalPdf(path, path.split("/")[0], Path(path).name, 100, 3, date(2021, 3, 12), digest, False)


def candidate(identifier: str = "abc-2021/प्र.क्र.75/अ", **changes):
    row = {
        "id": 1,
        "resolution_status": "NOT_IN_CORPUS",
        "normalized_document_number": identifier,
        "raw_document_number": identifier,
        "reference_kind": "GOVERNMENT_RESOLUTION",
        "normalized_reference_date": date(2021, 3, 12),
        "normalized_department": "finance department",
        "raw_department": "Finance Department",
        "source_document_id": 10,
        "source_file": "source.pdf",
        "candidate_relation_type": "",
        "blocked_by_page_quality": False,
    }
    row.update(changes)
    return row


def catalog(catalog_id: int = 1, identifier: str = "abc-2021/प्र.क्र.75/अ", **changes):
    row = {
        "id": catalog_id,
        "relative_path": f"Finance/{catalog_id}.pdf",
        "filename": f"{catalog_id}.pdf",
        "department_folder": "Finance Department",
        "normalized_department": normalize_department_alias("Finance Department"),
        "sha256": str(catalog_id) * 64,
        "document_kind": "GOVERNMENT_RESOLUTION",
        "normalized_document_number": identifier,
        "raw_document_number": identifier,
        "normalized_date": date(2021, 3, 12),
        "filename_date": date(2021, 3, 12),
        "identity_parse_status": "IDENTIFIED_EXACT",
        "identity_quality": "HIGH",
        "ingested_document_id": None,
    }
    row.update(changes)
    return row


def resolution(catalog_id: int = 1, **changes) -> LocalResolution:
    values = {
        "group_id": "group",
        "normalized_identifier": "abc-2021/प्र.क्र.75/अ",
        "reference_kind": "GOVERNMENT_RESOLUTION",
        "status": "RESOLVED_LOCAL_EXACT",
        "catalog_id": catalog_id,
        "candidate_catalog_ids": (catalog_id,),
        "method": "EXACT_IDENTIFIER_TEXT_DATE",
        "evidence": "evidence",
        "confidence": "HIGH",
        "validation_status": "AUTO_VERIFIED",
        "ingestion_eligibility": "READY_FOR_INGESTION",
        "source_candidate_ids": (1,),
        "source_document_ids": (10,),
        "source_files": ("source.pdf",),
        "reference_dates": (date(2021, 3, 12),),
        "reference_departments": ("finance department",),
        "relation_signals": (),
        "mentions": 1,
        "high_quality_mentions": 1,
    }
    values.update(changes)
    return LocalResolution(**values)


class IdentityExtractionTests(SimpleTestCase):
    def test_multiline_number_devanagari_digits_and_date_are_preserved(self):
        text = """विषय ओळ
महाराष्ट्र शासन
वित्त विभाग
शासन निर्णय क्रमांक: एबीसी-२०२१/
प्र.क्र.७५/अ
दिनांक: १२ मार्च २०२१
वाचा: जुना संदर्भ
"""
        raw, normalized, page, ambiguous = extract_multiline_document_number([(1, text)])
        self.assertIn("२०२१", raw)
        self.assertEqual(normalized, "एबीसी-2021/प्र.क्र.75/अ")
        self.assertEqual(page, 1)
        self.assertFalse(ambiguous)
        record = identity_from_pages(
            local_pdf(), [(1, text)], source_method="DIGITAL", digital_quality="GOOD_MARATHI", probe_pages=(1,)
        )
        self.assertEqual(record.identity_parse_status, "IDENTIFIED_EXACT")
        self.assertEqual(record.normalized_date, date(2021, 3, 12))
        self.assertEqual(record.document_kind, "GOVERNMENT_RESOLUTION")

    def test_bounded_header_does_not_capture_reference_number(self):
        text = "विषय\nमहाराष्ट्र शासन\nवाचा: शासन निर्णय क्रमांक ABC-2020/1"
        self.assertEqual(extract_multiline_document_number([(1, text)])[1], "")

    def test_unparseable_header_date_does_not_fall_through_to_reference_date(self):
        text = """महाराष्ट्र शासन
शासन निर्णय क्रमांक: एबीसी-2021/प्र.क्र.75/अ
वदिांक: ०6 एवप्रल, २०२२
संदभय: जुना आदेश वद.२८.०२.२०१२
"""
        record = identity_from_pages(
            local_pdf(), [(1, text)], source_method="DIGITAL", digital_quality="GOOD_MARATHI", probe_pages=(1,)
        )
        self.assertIsNone(record.normalized_date)
        self.assertIn("एवप्रल", record.raw_date)

    def test_document_type_catalogue_covers_observed_types(self):
        self.assertEqual(detect_document_kind("महाराष्ट्र शासन\nशासन परिपत्रक क्र. ABC/1"), "GOVERNMENT_CIRCULAR")
        self.assertEqual(detect_document_kind("CORRIGENDUM TO NOTIFICATION"), "CORRIGENDUM")
        self.assertEqual(detect_document_kind("Rights of Persons with Disabilities Act, 2016"), "ACT")
        self.assertEqual(detect_document_kind("Waste Management Rules 2018"), "RULE")

    def test_act_and_rule_canonical_identity(self):
        act = identity_from_pages(
            local_pdf(),
            [(1, "Rights of Persons with Disabilities Act, 2016")],
            source_method="DIGITAL",
            digital_quality="GOOD_ENGLISH",
            probe_pages=(1,),
        )
        self.assertEqual(act.document_kind, "ACT")
        self.assertIn("RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016", act.legal_canonical_key)
        self.assertEqual(act.identity_parse_status, "IDENTIFIED_EXACT")

    def test_ocr_fallback_stops_after_first_strong_identity(self):
        class Page:
            def extract_text(self):
                return ""

        class Reader:
            pages = [Page(), Page(), Page()]

        class Pdfium:
            def close(self):
                pass

        calls = []

        def ocr(_doc, index, try_rotations=False):
            calls.append(index)
            return "महाराष्ट्र शासन\nशासन निर्णय क्रमांक: एबीसी-2021/प्र.क्र.75/अ\nदिनांक: 12 मार्च 2021"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Finance" / "target.pdf"
            path.parent.mkdir()
            path.write_bytes(b"not read because PdfReader is mocked")
            item = local_pdf("Finance/target.pdf")
            with patch("knowledge_engine.corpus_identity.extraction.PdfReader", return_value=Reader()), patch(
                "pypdfium2.PdfDocument", return_value=Pdfium()
            ):
                record = probe_pdf_identity(
                    root,
                    item,
                    ocr_page_func=ocr,
                    tesseract_available_func=lambda: True,
                )
        self.assertEqual(record.identity_source_method, "OCR")
        self.assertEqual(record.ocr_pages, (1,))
        self.assertEqual(calls, [0])


class CatalogueLifecycleTests(SimpleTestCase):
    def test_idempotence_and_changed_file_detection(self):
        prior = {"sha256": "a" * 64, "catalog_version": CATALOG_VERSION}
        self.assertEqual(catalogue_action(None, sha256="a" * 64, resume=True), "NEW")
        self.assertEqual(catalogue_action(prior, sha256="a" * 64, resume=True), "UNCHANGED")
        self.assertEqual(catalogue_action(prior, sha256="b" * 64, resume=True), "CHANGED")
        self.assertEqual(catalogue_action(prior, sha256="a" * 64, resume=False), "CHANGED")

    def test_additive_migration_has_catalogue_and_no_fake_document_insert(self):
        sql = CorpusIdentityRepository().migration_sql().casefold()
        self.assertIn("create table if not exists local_document_catalog", sql)
        self.assertIn("resolved_target_catalog_id", sql)
        self.assertNotIn("insert into documents", sql)
        self.assertIn("idx_local_catalog_number_date", sql)


class LocalResolverTests(SimpleTestCase):
    def test_unique_exact_resolution_is_auto_verified(self):
        value = resolve_local_targets([candidate()], [catalog()])[0]
        self.assertEqual(value.status, "RESOLVED_LOCAL_EXACT")
        self.assertEqual(value.validation_status, "AUTO_VERIFIED")
        self.assertEqual(value.ingestion_eligibility, "READY_FOR_INGESTION")

    def test_partial_identity_requires_deterministic_corroboration(self):
        target = catalog(identity_parse_status="IDENTIFIED_PARTIAL")
        value = resolve_local_targets([candidate()], [target])[0]
        self.assertEqual(value.status, "RESOLVED_LOCAL_STRONG")
        no_date = {**target, "normalized_date": None, "filename_date": None, "normalized_department": "other"}
        unresolved = resolve_local_targets([candidate(normalized_reference_date=None, normalized_department="")], [no_date])[0]
        self.assertEqual(unresolved.status, "AMBIGUOUS_LOCAL")

    def test_number_date_disambiguation(self):
        targets = [catalog(1), catalog(2, normalized_date=date(2022, 1, 1), filename_date=date(2022, 1, 1))]
        value = resolve_local_targets([candidate()], targets)[0]
        self.assertEqual(value.catalog_id, 1)
        self.assertIn("TEXT_DATE", value.method)

    def test_number_department_disambiguation(self):
        targets = [
            catalog(1, normalized_date=None, filename_date=None),
            catalog(2, normalized_date=None, filename_date=None, normalized_department="housing department"),
        ]
        value = resolve_local_targets([candidate(normalized_reference_date=None)], targets)[0]
        self.assertEqual(value.catalog_id, 1)
        self.assertIn("DEPARTMENT", value.method)

    def test_duplicate_identifiers_remain_ambiguous(self):
        value = resolve_local_targets(
            [candidate(normalized_reference_date=None, normalized_department="")],
            [catalog(1, normalized_date=None), catalog(2, normalized_date=None)],
        )[0]
        self.assertEqual(value.status, "AMBIGUOUS_LOCAL")
        self.assertIsNone(value.catalog_id)

    def test_duplicate_sha_resolves_content_but_is_not_ingestion_ready(self):
        first = catalog(1, sha256="a" * 64)
        second = catalog(2, sha256="a" * 64)
        value = resolve_local_targets([candidate()], [first, second])[0]
        self.assertEqual(value.status, "RESOLVED_LOCAL_EXACT")
        self.assertEqual(value.ingestion_eligibility, "NOT_READY")

    def test_ingested_self_target_is_rejected_not_resolved(self):
        target = catalog(ingested_document_id=10)
        value = resolve_local_targets([candidate(source_document_id=10)], [target])[0]
        self.assertEqual(value.status, "SOURCE_UNPARSEABLE")
        self.assertEqual(value.method, "SELF_REFERENCE_REJECTED_LOCAL")
        self.assertEqual(value.validation_status, "REJECTED")
        self.assertIsNone(value.catalog_id)

    def test_not_found_kind_conflict_and_blocked_quality(self):
        self.assertEqual(resolve_local_targets([candidate()], [])[0].status, "NOT_FOUND_LOCAL")
        conflict = resolve_local_targets([candidate()], [catalog(document_kind="ACT")])[0]
        self.assertEqual(conflict.method, "DOCUMENT_KIND_CONFLICT")
        blocked = resolve_local_targets([candidate(blocked_by_page_quality=True)], [catalog()])[0]
        self.assertEqual(blocked.status, "BLOCKED_QUALITY")

    def test_filename_date_mismatch_is_not_treated_as_publication_proof(self):
        target = catalog(normalized_date=None, filename_date=date(2024, 2, 29))
        value = resolve_local_targets([candidate(normalized_reference_date=date(2020, 11, 12))], [target])[0]
        self.assertEqual(value.status, "AMBIGUOUS_LOCAL")
        self.assertEqual(value.method, "REFERENCE_DATE_NOT_CORROBORATED")

    def test_act_canonical_key_resolution(self):
        key = "rights_of_persons_with_disabilities_act_2016"
        target = catalog(
            identifier="",
            document_kind="ACT",
            normalized_document_number=key,
            legal_canonical_key="RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016",
        )
        value = resolve_local_targets([candidate(key, reference_kind="ACT")], [target])[0]
        self.assertEqual(value.status, "RESOLVED_LOCAL_EXACT")

    def test_structural_identifier_requires_date_corroboration(self):
        target = catalog(identifier="abc-2021/प्र.क्र.75/desk-2")
        matched = resolve_local_targets([candidate("abc-2021/प्र.क्र.75/desk-z")], [target])[0]
        self.assertEqual(matched.status, "RESOLVED_LOCAL_STRONG")
        self.assertIn("STRUCTURAL_IDENTIFIER_TEXT_DATE", matched.method)
        blocked = resolve_local_targets(
            [candidate("abc-2021/प्र.क्र.75/desk-z", normalized_reference_date=None, normalized_department="", raw_department="")],
            [{**target, "normalized_date": None, "filename_date": None}],
        )[0]
        self.assertEqual(blocked.status, "AMBIGUOUS_LOCAL")


class CompletionManifestTests(SimpleTestCase):
    def _pdf(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with path.open("wb") as handle:
            writer.write(handle)

    def test_manifest_aggregates_targets_and_excludes_ambiguous(self):
        rows = build_manifest(
            [resolution(), resolution(source_candidate_ids=(2,), source_document_ids=(11,), mentions=2), resolution(status="AMBIGUOUS_LOCAL", catalog_id=None, ingestion_eligibility="NOT_READY")],
            [catalog(sha256="a" * 64)],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["number_of_citing_documents"], 2)
        self.assertEqual(rows[0]["priority_tier"], "A")

    def test_manifest_path_sha_and_existing_document_safety(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Finance" / "target.pdf"
            self._pdf(path)
            import hashlib

            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            row = {"file_path": "Finance/target.pdf", "sha256": digest}
            self.assertEqual(validate_manifest([row], root=root, ingested_paths=set(), ingested_sha=set())["status"], "PASS")
            failed = validate_manifest([row, row], root=root, ingested_paths={"Finance/target.pdf"}, ingested_sha=set())
            self.assertEqual(failed["status"], "FAIL")
            self.assertEqual(failed["duplicate_paths"], 1)
            self.assertEqual(failed["already_ingested"], 2)


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="rollback-only PostgreSQL catalogue test")
def test_catalogue_target_persistence_is_additive_and_creates_no_document_row():
    repository = CorpusIdentityRepository()
    repository.apply_migration()
    with repository.connect() as connection:
        before_documents = connection.execute("SELECT count(*) n FROM documents").fetchone()["n"]
        candidate_row = connection.execute(
            "SELECT id,source_document_id FROM kg_reference_candidates WHERE resolution_status='NOT_IN_CORPUS' ORDER BY id LIMIT 1"
        ).fetchone()
        catalog_id = connection.execute(
            """INSERT INTO local_document_catalog(
                   relative_path,filename,department_folder,normalized_department,sha256,file_size,page_count,
                   document_kind,raw_document_number,normalized_document_number,identity_source_method,
                   identity_parse_status,identity_quality,catalog_version)
               VALUES ('integration/catalog.pdf','catalog.pdf','Integration','integration',%s,1,1,
                   'GOVERNMENT_RESOLUTION','ABC/2021/1','abc/2021/1','DIGITAL','IDENTIFIED_EXACT','HIGH',%s)
               RETURNING id""",
            ("f" * 64, CATALOG_VERSION),
        ).fetchone()["id"]
        value = resolution(
            catalog_id=int(catalog_id),
            source_candidate_ids=(int(candidate_row["id"]),),
            source_document_ids=(int(candidate_row["source_document_id"]),),
        )
        assert repository.persist_resolution(value, connection=connection) == 1
        stored = connection.execute(
            "SELECT resolved_target_catalog_id,local_resolution_status,local_validation_status FROM kg_reference_candidates WHERE id=%s",
            (candidate_row["id"],),
        ).fetchone()
        assert int(stored["resolved_target_catalog_id"]) == int(catalog_id)
        assert stored["local_validation_status"] == "AUTO_VERIFIED"
        assert connection.execute("SELECT count(*) n FROM documents").fetchone()["n"] == before_documents
        connection.rollback()
