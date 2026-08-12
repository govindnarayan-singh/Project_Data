from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .types import ParserStatus, ReferenceKind, ResolutionStatus, ValidationStatus


@dataclass(frozen=True, slots=True)
class ReferenceCandidate:
    source_document_id: int
    source_file: str
    source_department: str
    source_page: int
    reference_kind: ReferenceKind
    raw_evidence_text: str
    raw_document_number: str
    normalized_document_number: str
    raw_reference_date: str
    normalized_reference_date: date | None
    date_parse_status: str
    raw_department: str
    normalized_department: str
    pattern_id: str
    parser_version: str
    parser_status: ParserStatus
    parser_notes: str
    relation_signal: str
    candidate_relation_type: str
    evidence_hash: str
    fingerprint: str

    def with_parser_status(self, status: ParserStatus, note: str) -> "ReferenceCandidate":
        notes = "; ".join(value for value in (self.parser_notes, note) if value)
        return replace(self, parser_status=status, parser_notes=notes)


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolutionStatus
    target_document_id: int | None
    target_file: str
    method: str
    evidence: str
    candidate_count: int
    confidence_category: str
    validation_status: ValidationStatus
    blocked_by_page_quality: bool = False
    blocked_by_target_metadata: bool = False


@dataclass(frozen=True, slots=True)
class GraphRunResult:
    run_id: str
    documents_processed: int
    pages_processed: int
    candidate_count: int
    resolved_count: int
    reference_edge_count: int
    issued_by_edge_count: int
    parser_seconds: float
    resolver_seconds: float
    persistence_seconds: float
    wall_seconds: float
