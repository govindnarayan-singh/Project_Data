from __future__ import annotations

import hashlib
import json
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
DEPARTMENT_BEFORE_TYPE_RE = re.compile(
    r"(?:^|[;,:\)])\s*(?:\(?[०-९0-9]{1,3}\)?[.)]?\s*)?"
    r"(?P<department>[\u0900-\u097fA-Za-z][\u0900-\u097fA-Za-z &.'()/-]{1,110}?\s+विभाग|"
    r"[A-Za-z][A-Za-z &.'()/-]{1,110}?\s+Department)\s*,?\s*(?:चा|ची|चे|च्या)?\s*"
    r"(?=शासन\s+(?:निर्णय|परिपत्रक|पत्र|आदेश|शुद्धीपत्रक)|Government\s+(?:Resolution|Circular|Letter|Order))",
    re.IGNORECASE,
)
DEPARTMENT_AFTER_TYPE_RE = re.compile(
    r"(?:शासन\s+(?:निर्णय|परिपत्रक|पत्र|आदेश|शुद्धीपत्रक)|Government\s+(?:Resolution|Circular|Letter|Order))"
    r"\s*,?\s*(?P<department>[\u0900-\u097fA-Za-z][\u0900-\u097fA-Za-z &.'()/-]{1,110}?\s+विभाग|"
    r"[A-Za-z][A-Za-z &.'()/-]{1,110}?\s+Department)\s*,?\s*(?=(?:क्रमांक|क्र\.?|No\.?|Number))",
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

ISSUER_EXPLICIT_REFERENCE = "ISSUER_EXPLICIT_REFERENCE"
ISSUER_EXPLICIT_CONTEXT = "ISSUER_EXPLICIT_CONTEXT"
ISSUER_CONTROLLED_ALIAS = "ISSUER_CONTROLLED_ALIAS"
ISSUER_CITING_DOC_HINT_ONLY = "ISSUER_CITING_DOC_HINT_ONLY"
ISSUER_UNKNOWN = "ISSUER_UNKNOWN"

DEPARTMENT_CONTEXT_RE = re.compile(
    r"(?P<department>[\u0900-\u097fA-Za-z][\u0900-\u097fA-Za-z &.'()/-]{1,110}?(?:\s+विभाग|\s+Department))\s*$",
    re.IGNORECASE,
)
LEGAL_REFERENCE_RE = re.compile(
    r"(?P<title>[A-Za-z][A-Za-z0-9\s,.'&()\-/]{2,240}?\b(?:Act|Rules?))\s*,?\s*(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)
COMBINED_MAHARASHTRA_LOCAL_ACT_RE = re.compile(
    r"(?P<title>(?:"
    r"Maharashtra\s+Zilla\s+Parishads?\s+and\s+Panchayat\s+Samitis?\s+and\s+(?:the\s+)?Bombay\s+Village\s+Panchayats?"
    r"|Bombay\s+Village\s+Panchayats?\s*,?\s+and\s+(?:the\s+)?Maharashtra\s+Zilla\s+Parishads?\s+and\s+Panchayat\s+Samitis?)"
    r"\s*\((?:Second\s+|Third\s+)?Amendment\)\s*Act)\s*,?\s*(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE,
)
LEGAL_LEADING_NOISE_RE = re.compile(
    r"^.*?(?:may\s+be\s+called|commencement\s+of|under|omitted\s+by|amendments?\s+to)\s+(?:the\s+)?",
    re.IGNORECASE,
)
MULTI_REFERENCE_BOUNDARY_RE = re.compile(
    r"\s+(?:and|आणि)\s+(?=(?:Government\s+(?:Resolution|Circular|Order|Letter)|"
    r"शासन\s+(?:निर्णय|परिपत्रक|आदेश|पत्र)))",
    re.IGNORECASE,
)


def _bounded_lines(lines: list[str], index: int) -> tuple[list[str], list[str]]:
    """Return only structurally adjacent context, never arbitrary page text."""
    before = lines[max(0, index - 2):index]
    after = lines[index + 1:min(len(lines), index + 3)]
    return before, after


def _trim_legal_title(value: str) -> str:
    value = normalize_reference_whitespace(value).replace("\n", " ")
    value = re.sub(r"\[?(?:19|20)\d{2}\s*:\s*Mah\.\s*[IVXLCDM]+\]?", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bPari\s*sha\s*ds\b", "Parishads", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" []{}.;:—–-")
    value = LEGAL_LEADING_NOISE_RE.sub("", value)
    value = re.sub(r"^(?:PART\s+[IVXLCDM]+\s+)?(?:THE\s+)?", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^\d+\s+(?:THE\s+)?", "", value, flags=re.IGNORECASE)
    # Schedule headings describe an amendment operation; the cited identity is
    # the enactment named after TO, not a fictitious "Amendment To" Act.
    value = re.sub(r"^AMENDMENTS?\s+TO\s+(?:THE\s+)?", "", value, flags=re.IGNORECASE)
    # Section prose before an embedded title is not part of the enactment.
    marker_positions = []
    for marker in (
        "National Green Tribunal", "Environment (Protection)", "Water (Prevention",
        "Air (Prevention", "Maharashtra Zilla", "Maharashtra (Urban Areas)",
        "Maharashtra Slum Areas", "Scheduled Castes and Scheduled Tribes",
        "Bombay Village Panchayats",
    ):
        position = value.casefold().find(marker.casefold())
        if position >= 0:
            marker_positions.append(position)
    if marker_positions and min(marker_positions) > 0:
        value = value[min(marker_positions):]
    return value.strip(" []{}.;:—–-")


def reconstruct_legal_identifier(evidence: str, context_text: str, kind: ReferenceKind) -> tuple[str, str, list[str]]:
    """Recover a complete Act/Rule title from the same or adjacent source line."""
    rules: list[str] = []
    candidates: list[str] = []
    evidence_candidates: list[str] = []
    combined_candidates: list[str] = []
    for source_index, source in enumerate((evidence, context_text)):
        candidate_source = source.replace("\n", " ")
        candidate_source = re.sub(r"(?<=\b20)\s+(?=\d{2}\b)", "", candidate_source)
        candidate_source = re.sub(r"\b(Second|Third)Amendment\b", r"\1 Amendment", candidate_source, flags=re.IGNORECASE)
        candidate_source = re.sub(r"\)\s*Act\b", ") Act", candidate_source, flags=re.IGNORECASE)
        candidate_source = re.sub(
            r"\[?(?:19|20)\d{2}\s*:\s*Mah\.\s*[IVXLCDM]+\]?",
            " ", candidate_source, flags=re.IGNORECASE,
        )
        for match in COMBINED_MAHARASHTRA_LOCAL_ACT_RE.finditer(candidate_source):
            title = _trim_legal_title(f"{match.group('title')}, {match.group('year')}")
            if title and title not in candidates:
                candidates.append(title)
                combined_candidates.append(title)
                if source_index == 0:
                    evidence_candidates.append(title)
        for match in LEGAL_REFERENCE_RE.finditer(candidate_source):
            title = _trim_legal_title(f"{match.group('title')}, {match.group('year')}")
            if title and title not in candidates:
                candidates.append(title)
                if source_index == 0:
                    evidence_candidates.append(title)
    if not candidates:
        raw = evidence.strip(" .,:;-")
        return raw, normalize_legal_reference(raw, kind.value), rules
    incomplete = re.compile(
        r"^(?:\(|and\b|samitis?\b|clearance\b|preservation\b|tribunal\b|green\b|"
        r"pollution\b|protection\b|prevention\b|improvement\b|clearance\b|orders?\b|third\b|panchayat\b|parishads?\b|zilla\b|mah\b)",
        re.IGNORECASE,
    )
    evidence_is_fragment = bool(incomplete.search(normalize_reference_whitespace(evidence).lstrip("[]{}.,;:—–- ")))
    complete_evidence = [] if evidence_is_fragment else [item for item in evidence_candidates if not incomplete.search(item)]
    # A complete title in the reference itself outranks nearby titles. Context
    # is used only for an observed suffix/line-wrap fragment.
    raw = (
        max(combined_candidates, key=len) if combined_candidates
        else max(complete_evidence, key=len) if complete_evidence
        else max(candidates, key=len)
    )
    if normalize_reference_whitespace(raw).casefold() != normalize_reference_whitespace(evidence).casefold().strip(" .,:;-"):
        rules.append("LEGAL_TITLE_FROM_BOUNDED_CONTEXT")
    if re.search(r"^AMENDMENTS?\s+TO", evidence.strip(), re.IGNORECASE):
        rules.append("LEGAL_SCHEDULE_HEADING_TO_BASE_ACT")
    return raw, normalize_legal_reference(raw, kind.value), rules


def recover_context_issuer(
    evidence: str,
    before: list[str],
    *,
    citing_department: str = "",
) -> tuple[str, str, str, str, str, bool]:
    """Return raw/canonical issuer plus evidence state, text and location."""
    raw, normalized = extract_reference_department(evidence)
    if raw:
        return raw, normalized, ISSUER_EXPLICIT_REFERENCE, raw, "same_reference", normalized != normalize_reference_whitespace(raw).casefold()
    for offset, line in enumerate(reversed(before), start=1):
        match = DEPARTMENT_CONTEXT_RE.search(line)
        if not match:
            continue
        raw = match.group("department").strip(" ,.-")
        normalized = normalize_department_alias(raw)
        return raw, normalized, ISSUER_EXPLICIT_CONTEXT, line, f"previous_line_{offset}", normalized != normalize_reference_whitespace(raw).casefold()
    if citing_department:
        return "", "", ISSUER_CITING_DOC_HINT_ONLY, citing_department, "citing_document_metadata", False
    return "", "", ISSUER_UNKNOWN, "", "", False


def context_recovery_metadata(
    lines: list[str], index: int, evidence: str, kind: ReferenceKind, citing_department: str = "",
) -> dict[str, Any]:
    before, after = _bounded_lines(lines, index)
    context_text = "\n".join([*before, evidence, *after])
    raw_issuer, issuer, issuer_state, issuer_text, issuer_location, alias_applied = recover_context_issuer(
        evidence, before, citing_department=citing_department,
    )
    raw_identifier = normalized_identifier = ""
    rules: list[str] = []
    if kind in {ReferenceKind.ACT, ReferenceKind.RULE}:
        raw_identifier, normalized_identifier, rules = reconstruct_legal_identifier(evidence, context_text, kind)
    return {
        "context_before": "\n".join(before),
        "context_after": "\n".join(after),
        "context_text": context_text,
        "raw_identifier": raw_identifier,
        "normalized_identifier": normalized_identifier,
        "identifier_rules": rules,
        "raw_issuer": raw_issuer,
        "normalized_issuer": issuer,
        "issuer_state": issuer_state,
        "issuer_evidence_text": issuer_text,
        "issuer_evidence_location": issuer_location,
        "issuer_alias_applied": alias_applied,
    }


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
    value = MULTI_REFERENCE_BOUNDARY_RE.sub("; ", value)
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


def _extract_identifier(text: str, kind: ReferenceKind, context: dict[str, Any] | None = None) -> tuple[str, str]:
    if kind in {ReferenceKind.ACT, ReferenceKind.RULE}:
        if context and context.get("normalized_identifier"):
            return str(context["raw_identifier"]), str(context["normalized_identifier"])
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


def extract_reference_department(text: str) -> tuple[str, str]:
    """Extract an explicit issuer adjacent to the cited administrative document type."""
    normalized_text = normalize_reference_whitespace(text)
    match = DEPARTMENT_BEFORE_TYPE_RE.search(normalized_text) or DEPARTMENT_AFTER_TYPE_RE.search(normalized_text)
    if not match:
        return "", ""
    raw = match.group("department")
    raw = re.sub(r"^\s*(?:\(?[०-९0-9]{1,3}\)?[.)]?|[ivxlcdm]{1,6}[.)])\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(?:वाचा|संदर्भ|references?)\s*[:\-–—]*\s*", "", raw, flags=re.IGNORECASE)
    raw = raw.strip(" ,.-")
    return raw, normalize_department_alias(raw)


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
                    recovery = context_recovery_metadata(
                        lines, index, evidence, kind, str(document.get("department") or ""),
                    )
                    raw_number, normalized_number = _extract_identifier(evidence, kind, recovery)
                    raw_date, normalized_date = _extract_date(evidence)
                    raw_department = str(recovery["raw_issuer"])
                    normalized_department = str(recovery["normalized_issuer"])
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
                    recovery_note = {
                        "issuer_state": recovery["issuer_state"],
                        "issuer_evidence_location": recovery["issuer_evidence_location"],
                        "issuer_alias_applied": recovery["issuer_alias_applied"],
                        "identifier_rules": recovery["identifier_rules"],
                    }
                    notes = "; ".join(value for value in (notes, "V2=" + json.dumps(recovery_note, ensure_ascii=False, sort_keys=True)) if value)
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
                        date_parse_status=normalized_date.status, raw_department=raw_department,
                        normalized_department=normalized_department,
                        pattern_id=f"REF_{_language(evidence)}_{kind.value}_{context}", parser_version=self.version,
                        parser_status=status, parser_notes=notes, relation_signal=signal,
                        candidate_relation_type=candidate_relation, evidence_hash=evidence_hash, fingerprint=fp,
                    ))
        return result
