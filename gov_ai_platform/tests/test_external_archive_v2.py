from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pypdf import PdfWriter

from knowledge_engine.documents.sources import (
    EXTERNAL_OFFICIAL_ROOT,
    LOCAL_GR_ROOT,
    source_root_identity,
)
from knowledge_engine.external_acquisition.archive import (
    ARCHIVE_STATUS,
    ArchiveSafetyError,
    archive_directory,
    ensure_archive_root,
    promote_verified_file,
    resolve_archive_path,
    safe_archive_filename,
    validate_archive_relative_path,
)
from knowledge_engine.external_acquisition.v2 import MIGRATION, TARGET_SOURCE
from knowledge_engine.settings import PROJECT_ROOT


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _settings(tmp_path: Path) -> SimpleNamespace:
    quarantine = tmp_path / "quarantine"
    archive = tmp_path / "archive"
    gr_root = tmp_path / "grs"
    quarantine.mkdir()
    gr_root.mkdir()
    return SimpleNamespace(
        external_acquisition_root=quarantine,
        external_document_root=archive,
        gr_root=gr_root,
    )


def test_external_document_root_is_explicitly_configurable():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "EXTERNAL_DOCUMENT_ROOT=D:/LLM_Project_data/Government_Documents/External_Official" in example


def test_source_root_identity_is_closed_and_not_user_selectable():
    assert source_root_identity("LOCAL_ORIGINAL") == LOCAL_GR_ROOT
    assert source_root_identity("EXTERNAL_OFFICIAL") == EXTERNAL_OFFICIAL_ROOT
    with pytest.raises(ValueError):
        source_root_identity("C:/arbitrary")


def test_archive_root_is_created_and_rejects_overlap(tmp_path):
    fake = _settings(tmp_path)
    with patch("knowledge_engine.external_acquisition.archive.settings", fake):
        assert ensure_archive_root() == fake.external_document_root
        assert fake.external_document_root.is_dir()
        fake.external_document_root = fake.gr_root / "nested"
        with pytest.raises(ArchiveSafetyError, match="GR_ROOT"):
            ensure_archive_root()


@pytest.mark.parametrize(
    ("kind", "directory"),
    [
        ("ACT", "Acts"), ("RULE", "Rules"),
        ("GOVERNMENT_RESOLUTION", "Government_Resolutions"),
        ("CIRCULAR", "Circulars"), ("GOVERNMENT_CIRCULAR", "Circulars"),
        ("NOTIFICATION", "Notifications"), ("GOVERNMENT_ORDER", "Government_Orders"),
        ("COURT_ORDER", "Court_Documents"), ("UNKNOWN", "Other"),
    ],
)
def test_document_kinds_map_only_to_controlled_directories(kind, directory):
    assert archive_directory(kind) == directory


def test_safe_filename_is_deterministic_windows_safe_and_sha_suffixed():
    digest = "a" * 64
    first = safe_archive_filename("ACT", "Environment / Protection: Act 1986", digest)
    second = safe_archive_filename("ACT", "Environment / Protection: Act 1986", digest)
    assert first == second == "ACT_Environment_Protection_Act_1986_aaaaaaaaaaaa.pdf"
    assert not any(character in first for character in '<>:"/\\|?*')


def test_safe_filename_rejects_invalid_hash_and_never_emits_reserved_name():
    with pytest.raises(ArchiveSafetyError):
        safe_archive_filename("ACT", "CON", "bad")
    assert safe_archive_filename("CON", "", "b" * 64).upper() != "CON.PDF"


@pytest.mark.parametrize("value", ["../escape.pdf", "/absolute.pdf", "Acts\\bad.pdf", "Acts/file.txt"])
def test_archive_path_traversal_and_non_pdf_are_rejected(value):
    with pytest.raises(ArchiveSafetyError):
        validate_archive_relative_path(value)


def test_archive_promotion_is_byte_identical_immutable_and_same_sha_reuses(tmp_path):
    fake = _settings(tmp_path)
    source = fake.external_acquisition_root / "candidate.pdf"
    body = _pdf_bytes()
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    with patch("knowledge_engine.external_acquisition.archive.settings", fake):
        first = promote_verified_file(
            source, document_kind="ACT", identity="Example Act 2020", expected_sha256=digest
        )
        second = promote_verified_file(
            source, document_kind="ACT", identity="Example Act 2020", expected_sha256=digest
        )
        archived = resolve_archive_path(first.archive_relative_path, fake.external_document_root, must_exist=True)
    assert first.created is True
    assert second.created is False
    assert first.archive_status == ARCHIVE_STATUS
    assert archived.read_bytes() == body
    assert source.read_bytes() == body


def test_archive_promotion_rejects_sha_mismatch_and_non_incoming_source(tmp_path):
    fake = _settings(tmp_path)
    body = _pdf_bytes()
    source = fake.external_acquisition_root / "candidate.pdf"
    source.write_bytes(body)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(body)
    with patch("knowledge_engine.external_acquisition.archive.settings", fake):
        with pytest.raises(ArchiveSafetyError, match="SHA-256"):
            promote_verified_file(source, document_kind="ACT", identity="Act", expected_sha256="0" * 64)
        with pytest.raises(ArchiveSafetyError, match="Incoming"):
            promote_verified_file(
                outside, document_kind="ACT", identity="Act",
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )


def test_different_sha_archive_path_collision_is_rejected(monkeypatch, tmp_path):
    fake = _settings(tmp_path)
    first_body = _pdf_bytes()
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    stream = io.BytesIO()
    writer.write(stream)
    second_body = stream.getvalue()
    first = fake.external_acquisition_root / "first.pdf"
    second = fake.external_acquisition_root / "second.pdf"
    first.write_bytes(first_body)
    second.write_bytes(second_body)
    monkeypatch.setattr(
        "knowledge_engine.external_acquisition.archive.safe_archive_filename",
        lambda *_args, **_kwargs: "ACT_collision.pdf",
    )
    with patch("knowledge_engine.external_acquisition.archive.settings", fake):
        promote_verified_file(
            first, document_kind="ACT", identity="A",
            expected_sha256=hashlib.sha256(first_body).hexdigest(),
        )
        with pytest.raises(ArchiveSafetyError, match="collision"):
            promote_verified_file(
                second, document_kind="ACT", identity="B",
                expected_sha256=hashlib.sha256(second_body).hexdigest(),
            )


def test_v2_migration_and_frozen_target_contracts_are_additive():
    sql = MIGRATION.read_text(encoding="utf-8")
    for token in (
        "archive_source_root", "archive_relative_path", "original_remote_filename",
        "EXTERNAL_OFFICIAL_ARCHIVED", "ARCHIVE_FAILED", "external_document_provenance",
    ):
        assert token in sql
    assert "DROP TABLE" not in sql.upper()
    rows = list(__import__("csv").DictReader(TARGET_SOURCE.open("r", encoding="utf-8-sig", newline="")))
    assert len(rows) == 30
