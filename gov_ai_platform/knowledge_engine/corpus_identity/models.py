from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


CATALOG_VERSION = "local-corpus-identity-v1.0.2"


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    relative_path: str
    filename: str
    department_folder: str
    normalized_department: str
    sha256: str
    file_size: int
    file_mtime_ns: int
    page_count: int | None
    filename_date: date | None
    document_kind: str
    raw_document_number: str
    normalized_document_number: str
    raw_date: str
    normalized_date: date | None
    raw_department: str
    raw_subject: str
    subject_preview: str
    legal_canonical_key: str
    identity_source_page: int | None
    identity_source_method: str
    identity_parse_status: str
    identity_quality: str
    digital_quality: str
    probe_pages: tuple[int, ...] = field(default_factory=tuple)
    ocr_used: bool = False
    ocr_pages: tuple[int, ...] = field(default_factory=tuple)
    extraction_error: str = ""
    ingested_document_id: int | None = None
    catalog_version: str = CATALOG_VERSION
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class LocalResolution:
    group_id: str
    normalized_identifier: str
    reference_kind: str
    status: str
    catalog_id: int | None
    candidate_catalog_ids: tuple[int, ...]
    method: str
    evidence: str
    confidence: str
    validation_status: str
    ingestion_eligibility: str
    source_candidate_ids: tuple[int, ...]
    source_document_ids: tuple[int, ...]
    source_files: tuple[str, ...]
    reference_dates: tuple[date, ...]
    reference_departments: tuple[str, ...]
    relation_signals: tuple[str, ...]
    mentions: int
    high_quality_mentions: int
