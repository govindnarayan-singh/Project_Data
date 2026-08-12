from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from .models import ReferenceCandidate
from .normalization import (
    normalize_department_alias,
    normalize_document_number,
    normalize_legal_reference,
    normalize_reference_date,
    normalize_reference_whitespace,
)
from .types import PARSER_VERSION, ParserStatus, ReferenceKind


REFERENCE_HEADING_RE = re.compile(
    r"(?:^|\s)(संदर्भ|वाचा|references?)(?:\s*(?:क्रमांक|क्र\.?|no\.?)?)?\s*[:\-–—]",
    re.IGNORECASE,
)
MAIN_BODY_RE = re.compile(
    r"^(?:शासन\s+(?:निर्णय|परिपत्रक)|परिपत्रक|government\s+(?:resolution|circular))\s*[:\-–—]+\s*$",
    re.IGNORECASE,
)
ITEM_RE = re.compile(r"^\s*[-–—]?\s*(?:\(?[०-९0-9]{1,3}\)?[.)]?|[ivxlcdm]{1,6}[.)])\s+", re.IGNORECASE)
TYPE_PATTERNS: tuple[tuple[ReferenceKind, re.Pattern[str]], ...] = (
    (ReferenceKind.CORRIGENDUM, re.compile(r"शुद्धीपत्रक|corrigendum", re.IGNORECASE)),
    (ReferenceKind.GOVERNMENT_CIRCULAR, re.compile(r"शासन\s+परिपत्रक|government\s+circular", re.IGNORECASE)),
    (ReferenceKind.GOVERNMENT_RESOLUTION, re.compile(r"शासन\s+निर्णय|government\s+resolution|\bG\.?\s*R\.?\b", re.IGNORECASE)),
    (ReferenceKind.NOTIFICATION, re.compile(r"अधिसूचना|notification", re.IGNORECASE)),
    (ReferenceKind.ACT, re.compile(r"अधिनियम|\bact\b", re.IGNORECASE)),
    (ReferenceKind.COURT_ORDER, re.compile(r"(?:न्यायालय|न्यायाधिकरण|याचिका).{0,60}(?:आदेश|निर्णय)|court\s+order", re.IGNORECASE)),
    (ReferenceKind.GOVERNMENT_LETTER, re.compile(r"शासन\s+पत्र|government\s+letter", re.IGNORECASE)),
    (ReferenceKind.DEPARTMENT_LETTER, re.compile(r"(?:विभागाचे|समितीचे|यांचे)\s+पत्र|पत्र\s*\.?(?:क्र|क्रमांक)|(?:letter|F\.?\s*No\.?)\s+(?:no\.?)?", re.IGNORECASE)),
    (ReferenceKind.GOVERNMENT_ORDER, re.compile(r"शासन\s+आदेश|आदेश\s+क्रमांक|government\s+order|order\s+no\.?", re.IGNORECASE)),
    (ReferenceKind.RULE, re.compile(r"नियमावली|\bनियम\b|\brules?\b|regulations?", re.IGNORECASE)),
    (ReferenceKind.SUPPLEMENT, re.compile(r"पूरकपत्र|supplement", re.IGNORECASE)),
    (ReferenceKind.MEETING_MINUTES, re.compile(r"बैठकीचे\s+इतिवृत्त|minutes\s+of\s+(?:the\s+)?meeting", re.IGNORECASE)),
    (ReferenceKind.SCHEME_POLICY_DOCUMENT, re.compile(r"धोरण|policy|मार्गदर्शक\s+सूचना|guidelines?", re.IGNORECASE)),
    (ReferenceKind.CIRCULAR, re.compile(r"परिपत्रक|circular", re.IGNORECASE)),
)
DATE_RE = re.compile(
    r"(?:दिनांक|तारीख|[वल]?दि\.?|dated|Dt\.?)\s*[-:]?\s*"
    r"(\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s*,?\s*\d{4}|"
    r"\d{1,2}\s*[\u0900-\u097f]+\s*,?\s*\d{4})",
    re.IGNORECASE,
)
NUMBER_LABEL_RE = re.compile(
    r"(?:क्रमांक|क्र\.?|G\.?\s*R\.?\s*No\.?|F\.?\s*No\.?|No\.?|Number)\s*[:\-–—]*\s*([^,;\n]{2,220})",
    re.IGNORECASE,
)
ENGLISH_TYPE_NUMBER_RE = re.compile(
    r"(?:Government\s+(?:Resolution|Circular|Letter|Order)|Notification)\s*[-:]\s*([^,;\n]{2,220})",
    re.IGNORECASE,
)
MARATHI_TYPE_NUMBER_RE = re.compile(
    r"(?:शासन\s+(?:निर्णय|परिपत्रक|पत्र|आदेश|शुद्धीपत्रक))\s*[-:]\s*([^,;\n]{2,220})",
    re.IGNORECASE,
)
NUMBER_STRUCTURE_RE = re.compile(r"(?:/|प्र\s*\.\s*क्र|C\s*\.\s*R|\d{4}).*\d", re.IGNORECASE)
SOURCE_HEADER_RE = re.compile(
    r"^(?:शासन\s+(?:निर्णय|परिपत्रक|आदेश|शुद्धीपत्रक)|(?:government\s+)?(?:resolution|circular|order|notification|corrigendum))\s+(?:क्रमांक|क्र\.?|no\.?)",
    re.IGNORECASE,
)
BOILERPLATE_RE = re.compile(r"(?:संकेतस्थळ|website).{0,100}(?:आदेश|निर्णय|परिपत्रक)|(?:डिजीटल|digital)\s+स्वाक्षरी", re.IGNORECASE)
STOP_RE = re.compile(
    r"\s+(?:(?:दिनांक|तारीख|[वल]?दि\.?|dated|Dt\.?)\s*.*|"
    r"(?:प्रस्तावना|अन्वये|नुसार|रोजी|येथे|या\s+शिर्षाखाली|च्या\s+(?:शासन|परिपत्रक|अधिसूचना))\b.*)$",
    re.IGNORECASE,
)
RELATION_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"अधिक्रमित|supersed", re.IGNORECASE), "SUPERSEDES"),
    (re.compile(r"शुद्धीपत्रक|corrigendum", re.IGNORECASE), "CORRECTS"),
    (re.compile(r"मुदत\s*वाढ|मुदतवाढ|extend", re.IGNORECASE), "EXTENDS"),
    (re.compile(r"सुधारणा|दुरुस्ती|amend", re.IGNORECASE), "AMENDS"),
    (re.compile(r"रद्द|निरस्त|cancel", re.IGNORECASE), "CANCELS"),
)


def detect_reference_kind(text: str) -> ReferenceKind:
    normalized = normalize_reference_whitespace(text)
    for kind, pattern in TYPE_PATTERNS:
        if pattern.search(normalized):
            return kind
    return ReferenceKind.UNKNOWN


def _language(text: str) -> str:
    devanagari = any("\u0900" <= char <= "\u097f" for char in text)
    latin = any(char.isascii() and char.isalpha() for char in text)
    return "MIXED" if devanagari and latin else ("MARATHI" if devanagari else "ENGLISH")


def _section_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not REFERENCE_HEADING_RE.search(line):
            continue
        end = min(len(lines), index + 40)
        for candidate_end in range(index + 1, end):
            if MAIN_BODY_RE.fullmatch(lines[candidate_end]):
                end = candidate_end
                break
        ranges.append((index, end))
    return ranges


def _in_section(index: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _evidence_window(lines: list[str], index: int, in_section: bool) -> str:
    values = [lines[index]]
    for offset in (1, 2):
        if index + offset >= len(lines):
            break
        next_line = lines[index + offset]
        if not next_line or MAIN_BODY_RE.fullmatch(next_line) or ITEM_RE.match(next_line):
            break
        if in_section and (len(" ".join(values)) < 160 or DATE_RE.search(next_line)):
            values.append(next_line)
        else:
            break
    return " ".join(values)[:1200]


def _split_evidence(value: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s*;\s*", value) if part.strip()]
    return parts if len(parts) > 1 and all(detect_reference_kind(part) != ReferenceKind.UNKNOWN for part in parts) else [value]


def _extract_date(text: str):
    match = DATE_RE.search(text)
    raw = match.group(1).strip(" .,;") if match else ""
    return raw, normalize_reference_date(raw)


def _clean_identifier(value: str) -> str:
    value = STOP_RE.sub("", value)
    value = re.sub(r"\s+(?:प्रस्तावना|शासन\s+निर्णय)\s*[:\-].*$", "", value, flags=re.IGNORECASE)
    return value.strip(" :;,.–—-")


def _extract_identifier(text: str, kind: ReferenceKind) -> tuple[str, str]:
    if kind in {ReferenceKind.ACT, ReferenceKind.RULE}:
        raw = text.strip(" .,:;-")
        canonical = normalize_legal_reference(raw, kind.value)
        return raw, canonical
    match = NUMBER_LABEL_RE.search(text) or ENGLISH_TYPE_NUMBER_RE.search(text) or MARATHI_TYPE_NUMBER_RE.search(text)
    if not match:
        return "", ""
    raw = _clean_identifier(match.group(1))
    if not NUMBER_STRUCTURE_RE.search(normalize_reference_whitespace(raw)):
        return "", ""
    return raw, normalize_document_number(raw)


def _has_reference_evidence(text: str, in_section: bool) -> bool:
    if BOILERPLATE_RE.search(text):
        return False
    kind = detect_reference_kind(text)
    has_date = bool(DATE_RE.search(text))
    has_number = bool(NUMBER_LABEL_RE.search(text) or ENGLISH_TYPE_NUMBER_RE.search(text) or MARATHI_TYPE_NUMBER_RE.search(text))
    if kind in {ReferenceKind.ACT, ReferenceKind.RULE} and re.search(r"(?:19|20)[०-९0-9]{2}", normalize_reference_whitespace(text)):
        return True
    if in_section:
        return kind != ReferenceKind.UNKNOWN and (has_number or has_date)
    return kind != ReferenceKind.UNKNOWN and (has_number or has_date)


def _relation_signal(text: str) -> tuple[str, str]:
    for pattern, relation_type in RELATION_SIGNALS:
        match = pattern.search(text)
        if match:
            return match.group(0), relation_type
    return "", ""


def _fingerprint(source_document_id: int, source_page: int, kind: str, number: str, normalized_date: str, evidence_hash: str) -> str:
    # Raw evidence identity is stable when parser cleanup improves. Number/date
    # remain arguments for an explicit API but are deliberately not identity.
    payload = f"{source_document_id}|{source_page}|{kind}|{evidence_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReferenceParser:
    version = PARSER_VERSION

    def parse_document(self, document: dict[str, Any], pages: Iterable[dict[str, Any]]) -> list[ReferenceCandidate]:
        result: list[ReferenceCandidate] = []
        seen: set[str] = set()
        source_number = normalize_document_number(str(document.get("gr_number") or ""))
        source_date = str(document.get("gr_date") or "")
        for page in pages:
            lines = [normalize_reference_whitespace(line) for line in (page.get("text") or "").splitlines()]
            lines = [line for line in lines if line]
            ranges = _section_ranges(lines)
            for index, line in enumerate(lines):
                in_section = _in_section(index, ranges)
                if REFERENCE_HEADING_RE.fullmatch(line) or not _has_reference_evidence(line, in_section):
                    continue
                for evidence in _split_evidence(_evidence_window(lines, index, in_section)):
                    kind = detect_reference_kind(evidence)
                    raw_number, normalized_number = _extract_identifier(evidence, kind)
                    raw_date, normalized_date = _extract_date(evidence)
                    signal, candidate_relation = _relation_signal(evidence)
                    if not normalized_number and not normalized_date.value:
                        continue
                    self_reference = bool(
                        normalized_number and source_number and normalized_number == source_number
                        and (not normalized_date.value or normalized_date.iso_date == source_date)
                        and (not in_section or SOURCE_HEADER_RE.search(evidence))
                    )
                    if self_reference:
                        status = ParserStatus.SELF_REFERENCE_REJECTED
                        notes = "Identifier/date match the source metadata header"
                    elif not normalized_number:
                        status = ParserStatus.PARTIAL
                        notes = "No document identifier extracted"
                    elif normalized_date.status in {"INVALID", "UNPARSEABLE"}:
                        status = ParserStatus.NEEDS_REVIEW
                        notes = f"Reference date {normalized_date.status.lower()}"
                    elif len(normalized_number) < 5:
                        status = ParserStatus.NEEDS_REVIEW
                        notes = "Identifier is too short"
                    else:
                        status = ParserStatus.VALID
                        notes = ""
                    evidence_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
                    fp = _fingerprint(int(document["id"]), int(page["page_number"]), kind.value, normalized_number, normalized_date.iso_date, evidence_hash)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    context = "SECTION" if in_section else "INLINE"
                    result.append(ReferenceCandidate(
                        source_document_id=int(document["id"]), source_file=str(document["file_name"]),
                        source_department=str(document["department"]), source_page=int(page["page_number"]),
                        reference_kind=kind, raw_evidence_text=evidence,
                        raw_document_number=raw_number, normalized_document_number=normalized_number,
                        raw_reference_date=raw_date, normalized_reference_date=normalized_date.value,
                        date_parse_status=normalized_date.status, raw_department="",
                        normalized_department=normalize_department_alias(""),
                        pattern_id=f"REF_{_language(evidence)}_{kind.value}_{context}", parser_version=self.version,
                        parser_status=status, parser_notes=notes, relation_signal=signal,
                        candidate_relation_type=candidate_relation, evidence_hash=evidence_hash, fingerprint=fp,
                    ))
        return result
