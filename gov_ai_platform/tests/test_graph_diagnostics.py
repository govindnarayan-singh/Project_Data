from __future__ import annotations

from dataclasses import replace

from knowledge_engine.graph.diagnostics.extractor import (
    detect_reference_type,
    extract_reference_candidates,
)
from knowledge_engine.graph.diagnostics.models import ReferenceCandidate
from knowledge_engine.graph.diagnostics.normalization import (
    devanagari_to_ascii_digits,
    normalize_date,
    normalize_document_number,
    normalize_gr_number,
    normalize_reference_text,
)
from knowledge_engine.graph.diagnostics.resolution import DiagnosticResolver


def _document(document_id: int = 1, number: str = "SOURCE/2024/1", department: str = "Test") -> dict:
    return {
        "id": document_id,
        "file_name": f"{document_id}.pdf",
        "department": department,
        "gr_number": number,
        "gr_date": "2024-01-01",
    }


def _page(text: str, page_number: int = 1) -> dict:
    return {"page_number": page_number, "text": text}


def _candidate(number: str = "ABC-2020/1", date: str = "") -> ReferenceCandidate:
    return ReferenceCandidate(
        candidate_id="REF-test",
        source_document_id=1,
        source_file="1.pdf",
        source_department="Test",
        source_page=1,
        reference_type="GOVERNMENT_RESOLUTION",
        language="ENGLISH",
        structural_context="REFERENCE_SECTION",
        raw_reference_text="Government Resolution No. ABC/2020/1",
        extracted_document_number=number,
        normalized_document_number=normalize_document_number(number),
        extracted_date=date,
        normalized_date=normalize_date(date).iso_date,
        date_parse_status=normalize_date(date).status,
        extracted_department="",
        extraction_pattern_id="REF_EN_GR_SECTION",
        extraction_method="DETERMINISTIC",
        parser_status="COMPLETE",
        parser_notes="",
    )


def test_devanagari_digit_normalization() -> None:
    assert devanagari_to_ascii_digits("क्र. १२३/२०२४") == "क्र. 123/2024"


def test_whitespace_normalization() -> None:
    assert normalize_reference_text("  शासन\t निर्णय\u00a0 क्रमांक  ") == "शासन निर्णय क्रमांक"


def test_slash_and_hyphen_normalization() -> None:
    assert normalize_document_number("ABC - 2024 / १२ / X") == "abc2024/12/x"


def test_gr_number_normalization_preserves_matching_semantics() -> None:
    raw = "शासन निर्णय क्रमांक : दिव्यांग-२०२३ / प्र. क्र. ५६ / दि.क.२"
    assert normalize_gr_number(raw) == "दिव्यांग2023/प्र.क्र.56/दि.क.2"


def test_marathi_reference_detection() -> None:
    assert detect_reference_type("शासन परिपत्रक क्र. प्रानिम-१२१८/प्र.क्र.२७") == "GOVERNMENT_CIRCULAR"


def test_english_reference_detection() -> None:
    assert detect_reference_type("Government Resolution No. GAD-IT/2023/4") == "GOVERNMENT_RESOLUTION"


def test_reference_date_extraction_formats() -> None:
    assert normalize_date("१२ ऑक्टोबर, २०२०").iso_date == "2020-10-12"
    assert normalize_date("5th Oct 2023").iso_date == "2023-10-05"
    assert normalize_date("14/03/2018").iso_date == "2018-03-14"


def test_source_header_false_positive_is_rejected() -> None:
    candidates = extract_reference_candidates(
        _document(number="ABC/2024/1"),
        [_page("शासन निर्णय क्रमांक : ABC/2024/1 दिनांक ०१/०१/२०२४")],
    )
    assert candidates == []


def test_multiline_reference_is_combined() -> None:
    text = """संदर्भ :-
१) शासन निर्णय क्रमांक ABC-२०२०/प्र.क्र.१
दिनांक १२ ऑक्टोबर, २०२०
शासन निर्णय :-"""
    candidates = extract_reference_candidates(_document(), [_page(text)])
    assert len(candidates) == 1
    assert candidates[0].normalized_document_number == "abc2020/प्र.क्र.1"
    assert candidates[0].normalized_date == "2020-10-12"


def test_duplicate_reference_on_same_page_is_deduplicated() -> None:
    line = "१) शासन निर्णय क्रमांक ABC/२०२०/१ दिनांक १२/१०/२०२०"
    candidates = extract_reference_candidates(_document(), [_page(f"संदर्भ :-\n{line}\n{line}")])
    assert len(candidates) == 1


def test_exact_target_resolution() -> None:
    resolver = DiagnosticResolver([_document(), _document(2, "ABC-2020/1")])
    result = resolver.resolve(_candidate())
    assert result.resolution_status == "RESOLVED_EXACT"
    assert result.target_document_id == 2


def test_ambiguous_target_resolution() -> None:
    resolver = DiagnosticResolver(
        [_document(), _document(2, "ABC-2020/1"), _document(3, "ABC—2020/1")]
    )
    result = resolver.resolve(_candidate())
    assert result.resolution_status == "AMBIGUOUS"
    assert result.candidate_count == 2


def test_unresolved_reference_handling() -> None:
    resolver = DiagnosticResolver([_document()])
    assert resolver.resolve(_candidate("MISSING/2020/9")).resolution_status == "NOT_IN_CORPUS"
    missing_number = replace(
        _candidate(), extracted_document_number="", normalized_document_number=""
    )
    assert resolver.resolve(missing_number).resolution_status == "UNPARSEABLE"
