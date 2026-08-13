from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class LocalPdf:
    relative_path: str
    department: str
    file_name: str
    file_size: int
    page_count: int | None
    filename_date: date | None
    sha256: str
    already_ingested: bool
    inventory_error: str = ""


@dataclass(frozen=True, slots=True)
class UnresolvedGroup:
    group_id: str
    normalized_identifier: str
    reference_kind: str
    raw_identifiers: tuple[str, ...]
    reference_dates: tuple[date, ...]
    source_departments: tuple[str, ...]
    source_documents: tuple[int, ...]
    source_files: tuple[str, ...]
    source_pages: tuple[str, ...]
    relation_signals: tuple[str, ...]
    mentions: int
    high_quality_mentions: int
    blocked_mentions: int


@dataclass(frozen=True, slots=True)
class MetadataProbe:
    relative_path: str
    department: str
    file_name: str
    sha256: str
    page_count: int | None
    pages_probed: int
    probe_method: str
    raw_identifier: str
    normalized_identifier: str
    document_date: date | None
    document_type: str
    subject: str
    text_characters: int
    probe_error: str = ""


@dataclass(frozen=True, slots=True)
class AcquisitionMatch:
    group: UnresolvedGroup
    status: str
    candidates: tuple[MetadataProbe, ...] = field(default_factory=tuple)
    match_method: str = ""
    matching_evidence: str = ""
    confidence_category: str = "LOW"
    priority_score: int = 0
