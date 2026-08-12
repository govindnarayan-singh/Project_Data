from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReferenceCandidate:
    candidate_id: str
    source_document_id: int
    source_file: str
    source_department: str
    source_page: int
    reference_type: str
    language: str
    structural_context: str
    raw_reference_text: str
    extracted_document_number: str
    normalized_document_number: str
    extracted_date: str
    normalized_date: str
    date_parse_status: str
    extracted_department: str
    extraction_pattern_id: str
    extraction_method: str
    parser_status: str
    parser_notes: str


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    candidate_id: str
    resolution_status: str
    target_document_id: int | None
    target_file: str
    resolution_method: str
    resolution_evidence: str
    candidate_count: int
    confidence_category: str

