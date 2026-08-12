from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from .models import ReferenceCandidate
from .normalization import normalize_date, normalize_document_number, normalize_reference_text


REFERENCE_HEADING_RE = re.compile(
    r"(?:^|\s)(संदर्भ|वाचा|references?)(?:\s*(?:क्रमांक|क्र\.?|no\.?)?)?\s*[:\-–—]",
    re.IGNORECASE,
)
MAIN_BODY_RE = re.compile(
    r"^(?:शासन\s+(?:निर्णय|परिपत्रक)|परिपत्रक|government\s+(?:resolution|circular))\s*[:\-–—]+\s*$",
    re.IGNORECASE,
)
ITEM_START_RE = re.compile(
    r"^\s*[-–—]?\s*(?:\(?\d{1,3}\)?[.)]?|[ivxlcdm]{1,6}[.)])\s+",
    re.IGNORECASE,
)
TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CORRIGENDUM", re.compile(r"शुद्धीपत्रक|corrigendum", re.IGNORECASE)),
    ("GOVERNMENT_CIRCULAR", re.compile(r"शासन\s+परिपत्रक|government\s+circular", re.IGNORECASE)),
    ("GOVERNMENT_RESOLUTION", re.compile(r"शासन\s+निर्णय|government\s+resolution|\bG\.?\s*R\.?\b", re.IGNORECASE)),
    ("NOTIFICATION", re.compile(r"अधिसूचना|notification", re.IGNORECASE)),
    ("ACT", re.compile(r"अधिनियम|\bact\b", re.IGNORECASE)),
    ("RULE", re.compile(r"नियमावली|\bनियम\b|\brules?\b", re.IGNORECASE)),
    ("COURT_ORDER", re.compile(r"(?:न्यायालय|न्यायाधिकरण|याचिका).{0,50}(?:आदेश|निर्णय)|court\s+order", re.IGNORECASE)),
    ("GOVERNMENT_LETTER", re.compile(r"शासन\s+पत्र|government\s+letter", re.IGNORECASE)),
    ("DEPARTMENT_LETTER", re.compile(r"(?:यांचे|विभागाचे|समितीचे)\s+(?:पत्र|letter)|\bपत्र\s+क्र\.?|letter\s+no\.?", re.IGNORECASE)),
    ("GOVERNMENT_ORDER", re.compile(r"शासन\s+आदेश|आदेश\s+क्रमांक|government\s+order|order\s+no\.?", re.IGNORECASE)),
    ("SUPPLEMENT", re.compile(r"पूरकपत्र|supplement", re.IGNORECASE)),
    ("MEETING_MINUTES", re.compile(r"बैठकीचे\s+इतिवृत्त|minutes\s+of\s+(?:the\s+)?meeting", re.IGNORECASE)),
    ("SCHEME_DOCUMENT", re.compile(r"धोरण|policy|मार्गदर्शक\s+सूचना|guidelines?", re.IGNORECASE)),
)
IDENTIFIER_LABEL_RE = re.compile(
    r"(?:क्रमांक|क्र\.?|No\.?|Number)\s*[:\-–—]*\s*([^,;\n]{2,180})",
    re.IGNORECASE,
)
ENGLISH_AFTER_TYPE_RE = re.compile(
    r"(?:Government\s+(?:Resolution|Circular|Letter)|Notification|Order)\s*[-:]\s*([^,;\n]{2,180})",
    re.IGNORECASE,
)
DATE_FRAGMENT_RE = re.compile(
    r"(?:दिनांक|[वल]?दि\.?|dated|date\s*:)\s*[-:]?\s*"
    r"(\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s*,?\s*\d{4}|"
    r"\d{1,2}\s*[\u0900-\u097f]+\s*,?\s*\d{4})",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
NUMBERISH_RE = re.compile(r"[A-Za-z\u0900-\u097f].{0,70}(?:/|प्र\.\s*क्र|C\.\s*R\.).{0,70}\d", re.IGNORECASE)
SELF_HEADER_RE = re.compile(
    r"^(?:शासन\s+(?:निर्णय|परिपत्रक|आदेश|शुद्धीपत्रक)|(?:government\s+)?(?:resolution|circular|order|notification|corrigendum))\s+(?:क्रमांक|क्र\.?|no\.?)",
    re.IGNORECASE,
)
BOILERPLATE_RE = re.compile(
    r"(?:संकेतांक|संगणक\s+संकेतांक|website|संकेतस्थळ).{0,100}(?:आदेश|निर्णय|परिपत्रक)|"
    r"(?:डिजीटल|digital)\s+स्वाक्षरी",
    re.IGNORECASE,
)


def detect_reference_type(text: str) -> str:
    normalized = normalize_reference_text(text)
    for reference_type, pattern in TYPE_PATTERNS:
        if pattern.search(normalized):
            return reference_type
    return "OTHER_DOCUMENT"


def detect_language(text: str) -> str:
    devanagari = sum("\u0900" <= character <= "\u097f" for character in text)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    if devanagari and latin:
        return "MIXED"
    if devanagari:
        return "MARATHI"
    return "ENGLISH"


def _extract_number(text: str, reference_type: str) -> str:
    normalized = normalize_reference_text(text)
    match = IDENTIFIER_LABEL_RE.search(normalized) or ENGLISH_AFTER_TYPE_RE.search(normalized)
    if match:
        value = match.group(1)
        value = re.split(r"\s+(?:दिनांक|दि\.?|dated|date\s*:)", value, maxsplit=1, flags=re.IGNORECASE)[0]
        value = re.split(r"\s+(?:रोजी|अन्वये|चा|ची|चे)\b", value, maxsplit=1)[0]
        value = value.strip(" :;,.–—-")
        if value and (NUMBERISH_RE.search(value) or re.search(r"\d{3,}", value)):
            return value
    if reference_type in {"ACT", "RULE"}:
        year = YEAR_RE.search(normalized)
        type_word = re.search(r"[^.;\n]{0,100}(?:अधिनियम|नियमावली|\bAct\b|\bRules?\b)", normalized, re.IGNORECASE)
        if year and type_word:
            return f"{type_word.group(0).strip()} {year.group(0)}"
    return ""


def _extract_date(text: str) -> tuple[str, str, str]:
    normalized = normalize_reference_text(text)
    match = DATE_FRAGMENT_RE.search(normalized)
    raw = match.group(1).strip(" .,;") if match else ""
    parsed = normalize_date(raw)
    return raw, parsed.iso_date, parsed.status


def _has_reference_evidence(text: str, in_section: bool) -> bool:
    normalized = normalize_reference_text(text)
    if SELF_HEADER_RE.search(normalized) or BOILERPLATE_RE.search(normalized):
        return False
    reference_type = detect_reference_type(normalized)
    has_type = reference_type != "OTHER_DOCUMENT"
    has_identifier = bool(IDENTIFIER_LABEL_RE.search(normalized) or ENGLISH_AFTER_TYPE_RE.search(normalized) or NUMBERISH_RE.search(normalized))
    has_date = bool(DATE_FRAGMENT_RE.search(normalized))
    if reference_type in {"ACT", "RULE"} and YEAR_RE.search(normalized):
        return True
    if in_section and (has_type or (ITEM_START_RE.match(normalized) and has_identifier)) and (has_date or has_identifier):
        return True
    return has_type and (has_identifier or has_date)


def _section_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not REFERENCE_HEADING_RE.search(normalize_reference_text(line)):
            continue
        end = min(len(lines), index + 35)
        for candidate_end in range(index + 1, end):
            if MAIN_BODY_RE.match(normalize_reference_text(lines[candidate_end])):
                end = candidate_end
                break
        ranges.append((index, end))
    return ranges


def _in_ranges(index: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _evidence_window(lines: list[str], index: int, in_section: bool) -> str:
    selected = [lines[index].strip()]
    for offset in (1, 2):
        next_index = index + offset
        if next_index >= len(lines):
            break
        next_line = lines[next_index].strip()
        normalized = normalize_reference_text(next_line)
        if not next_line or MAIN_BODY_RE.match(normalized) or ITEM_START_RE.match(normalized):
            break
        if in_section and (DATE_FRAGMENT_RE.search(normalized) or len(" ".join(selected)) < 120):
            selected.append(next_line)
        else:
            break
    return " ".join(selected)[:1000]


def _pattern_id(reference_type: str, language: str, context: str) -> str:
    type_codes = {
        "GOVERNMENT_RESOLUTION": "GR",
        "GOVERNMENT_CIRCULAR": "CIRCULAR",
        "NOTIFICATION": "NOTIFICATION",
        "ACT": "ACT",
        "RULE": "RULE",
        "GOVERNMENT_LETTER": "GOV_LETTER",
        "DEPARTMENT_LETTER": "LETTER",
        "GOVERNMENT_ORDER": "ORDER",
        "COURT_ORDER": "COURT",
        "CORRIGENDUM": "CORRIGENDUM",
        "SUPPLEMENT": "SUPPLEMENT",
        "MEETING_MINUTES": "MINUTES",
        "SCHEME_DOCUMENT": "SCHEME",
        "OTHER_DOCUMENT": "OTHER",
    }
    language_code = {"MARATHI": "MR", "ENGLISH": "EN", "MIXED": "MIXED"}[language]
    context_code = {"REFERENCE_SECTION": "SECTION", "INLINE": "INLINE"}[context]
    return f"REF_{language_code}_{type_codes[reference_type]}_{context_code}"


def extract_reference_candidates(
    document: dict[str, Any], pages: Iterable[dict[str, Any]]
) -> list[ReferenceCandidate]:
    candidates: list[ReferenceCandidate] = []
    seen: set[tuple[int, str, str, str]] = set()
    source_number = normalize_document_number(str(document.get("gr_number") or ""))
    for page in pages:
        raw_lines = [line.strip() for line in (page.get("text") or "").splitlines() if line.strip()]
        ranges = _section_ranges(raw_lines)
        for index, raw_line in enumerate(raw_lines):
            in_section = _in_ranges(index, ranges)
            normalized_line = normalize_reference_text(raw_line)
            if REFERENCE_HEADING_RE.fullmatch(normalized_line):
                continue
            if not _has_reference_evidence(raw_line, in_section):
                continue
            evidence = _evidence_window(raw_lines, index, in_section)
            reference_type = detect_reference_type(evidence)
            extracted_number = _extract_number(evidence, reference_type)
            normalized_number = normalize_document_number(extracted_number)
            raw_date, normalized_date, date_status = _extract_date(evidence)
            if not in_section and normalized_number and normalized_number == source_number:
                continue
            if not normalized_number and not normalized_date:
                continue
            context = "REFERENCE_SECTION" if in_section else "INLINE"
            language = detect_language(evidence)
            pattern_id = _pattern_id(reference_type, language, context)
            key = (int(page["page_number"]), reference_type, normalized_number, normalized_date)
            if key in seen:
                continue
            seen.add(key)
            digest = hashlib.sha1(
                f"{document['id']}|{page['page_number']}|{reference_type}|{normalized_number}|{normalized_date}|{evidence}".encode("utf-8")
            ).hexdigest()[:12]
            parser_status = "COMPLETE" if normalized_number and normalized_date else "PARTIAL"
            notes = []
            if not normalized_number:
                notes.append("document number not extracted")
            if not normalized_date:
                notes.append("date not extracted")
            candidates.append(
                ReferenceCandidate(
                    candidate_id=f"REF-{digest}",
                    source_document_id=int(document["id"]),
                    source_file=str(document["file_name"]),
                    source_department=str(document["department"]),
                    source_page=int(page["page_number"]),
                    reference_type=reference_type,
                    language=language,
                    structural_context=context,
                    raw_reference_text=evidence,
                    extracted_document_number=extracted_number,
                    normalized_document_number=normalized_number,
                    extracted_date=raw_date,
                    normalized_date=normalized_date,
                    date_parse_status=date_status,
                    extracted_department="",
                    extraction_pattern_id=pattern_id,
                    extraction_method="DETERMINISTIC",
                    parser_status=parser_status,
                    parser_notes="; ".join(notes),
                )
            )
    return candidates
