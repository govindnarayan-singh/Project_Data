from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from knowledge_engine.acquisition.inventory import resolve_inside_root
from knowledge_engine.acquisition.models import LocalPdf
from knowledge_engine.extraction.pdf import _ocr_page, _tesseract_available
from knowledge_engine.extraction.text_quality import (
    digital_text_classification,
    normalize_marathi_text,
)
from knowledge_engine.graph.normalization import (
    normalize_department_alias,
    normalize_document_number,
    normalize_legal_reference,
    normalize_reference_date,
)

from .models import IdentityRecord


HEADER_STOP_RE = re.compile(
    r"^(?:वाचा|वयचय|िाचा|संदर्भ|संदभय|प्रस्तावना|प्रस्ताििा|शासन\s+(?:निर्णय|आदेश)\s*[:\-]|read\s*[:\-]|references?\s*[:\-])",
    re.IGNORECASE,
)
NUMBER_LABEL_RE = re.compile(
    r"(?:क्रमांक|क्रमांकः|क्र\.?|no\.?|number|g\.?\s*r\.?\s*no\.?)\s*[:\-–—]*\s*(?P<number>.+)$",
    re.IGNORECASE,
)
NUMBER_STRUCTURE_RE = re.compile(r"(?=.*\d)(?=.*[/.-])[0-9A-Za-z\u0900-\u097f]", re.DOTALL)
DATE_PREFIX_RE = re.compile(
    r"(?:दिनांक|ददनांक|शदनांक|वदनांक|दिियांक|वदिांक|ददियांक|दि\.?|वद\.?|शद\.?|date)\s*[:\-]?",
    re.IGNORECASE,
)
LEGAL_MARKER_RE = re.compile(r"(?:अधिनियम|अदिदि्म|नियमावली|\bact\b|\brules?\b)", re.IGNORECASE)

KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CORRIGENDUM", re.compile(r"(?:शुद्धीपत्रक|शुध्दीपत्रक|corrigendum)", re.IGNORECASE)),
    ("GOVERNMENT_CIRCULAR", re.compile(r"(?:शासन\s+परिपत्रक|सयरि\s+पदरपत्रक|government\s+circular)", re.IGNORECASE)),
    ("CIRCULAR", re.compile(r"(?:परिपत्रक|पशरपत्रक|पदरपत्रक|\bcircular\b)", re.IGNORECASE)),
    ("GOVERNMENT_RESOLUTION", re.compile(r"(?:शासन\s+(?:निर्णय|वनणधय)|िासन\s+(?:शनणगय|विणथय)|सयरि\s+दिणग्|government\s+resolution|\bG\.?R\.?)", re.IGNORECASE)),
    ("NOTIFICATION", re.compile(r"(?:अधिसूचना|अदिरूचिय|\bnotification\b)", re.IGNORECASE)),
    ("GOVERNMENT_ORDER", re.compile(r"(?:शासन\s+आदेश|government\s+order)", re.IGNORECASE)),
    ("COURT_ORDER", re.compile(r"(?:न्यायालयीन\s+आदेश|court\s+order)", re.IGNORECASE)),
    ("MEETING_MINUTES", re.compile(r"(?:बैठकीचे\s+इतिवृत्त|minutes\s+of\s+meeting)", re.IGNORECASE)),
    ("SCHEME_POLICY_DOCUMENT", re.compile(r"(?:धोरण|policy\s+document|scheme\s+document)", re.IGNORECASE)),
    ("ACT", re.compile(r"(?:अधिनियम|अदिदि्म|\bact\b)", re.IGNORECASE)),
    ("RULE", re.compile(r"(?:नियमावली|\brules?\b)", re.IGNORECASE)),
)

BAD_DIGITAL_CLASSES = {
    "EMPTY_OR_IMAGE_ONLY",
    "CORRUPTED_MARATHI",
    "SUSPICIOUS_GARBLED_LATIN",
    "UNCERTAIN",
}


def detect_document_kind(text: str) -> str:
    header = normalize_marathi_text(text)[:8000]
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(header):
            return kind
    return "UNKNOWN"


def _header_lines(text: str, limit: int = 70) -> list[str]:
    result: list[str] = []
    for line in normalize_marathi_text(text).splitlines():
        line = line.strip(" \t:,-–—")
        if not line:
            continue
        if HEADER_STOP_RE.search(line):
            break
        result.append(line)
        if len(result) >= limit:
            break
    return result


def _continuation_is_identifier(line: str) -> bool:
    if not line or len(line) > 100 or HEADER_STOP_RE.search(line):
        return False
    return bool(re.fullmatch(r"[0-9A-Za-z\u0900-\u097f./()\-–—\s]+[,;]?", line)) and bool(
        re.search(r"[/\d\u0966-\u096f]", line)
    )


def extract_multiline_document_number(page_texts: list[tuple[int, str]]) -> tuple[str, str, int | None, bool]:
    """Extract only bounded header identifiers and preserve their raw form.

    At most two continuation lines are joined, and only when the first fragment
    ends in a slash/hyphen or the following line itself has identifier structure.
    """
    candidates: list[tuple[str, str, int]] = []
    for page_number, text in page_texts:
        lines = _header_lines(text)
        for index, line in enumerate(lines):
            match = NUMBER_LABEL_RE.search(line)
            if not match:
                continue
            raw = match.group("number").strip(" \t,;:-")
            joined = [raw]
            for following in lines[index + 1 : index + 3]:
                if not (_continuation_is_identifier(following) and (joined[-1].rstrip().endswith(("/", "-", "–", "—")) or "/" in following)):
                    break
                joined.append(following.strip(" \t,;"))
            raw = "".join(joined)
            normalized = normalize_document_number(raw)
            if normalized:
                candidates.append((raw, normalized, page_number))
            break
    if not candidates:
        return "", "", None, False
    earliest_page = min(value[2] for value in candidates)
    earliest = [value for value in candidates if value[2] == earliest_page]
    unique = {value[1] for value in earliest}
    raw, normalized, page = earliest[0]
    return raw, normalized, page, len(unique) > 1


def _extract_date(page_texts: list[tuple[int, str]], identity_page: int | None) -> tuple[str, date | None]:
    ordered = [item for item in page_texts if item[0] == identity_page] or page_texts[:1]
    for _, text in ordered:
        for line in _header_lines(text, limit=30):
            if not DATE_PREFIX_RE.search(line):
                continue
            repaired = re.sub(r"(?<=[\u0900-\u097f])\s+(?=[\u093a-\u094d])", "", line)
            repaired = re.sub(r"(?<=[\u093a-\u094d])\s+(?=[\u0900-\u097f])", "", repaired)
            parsed = normalize_reference_date(repaired)
            if parsed.value:
                return line, parsed.value
            # This is the document header's own date label. Continuing into a
            # reference list would turn a cited date into false target evidence.
            return line, None
    return "", None


def _extract_subject_and_department(page_texts: list[tuple[int, str]], folder_department: str) -> tuple[str, str]:
    if not page_texts:
        return "", folder_department
    lines = [line for line in normalize_marathi_text(page_texts[0][1]).splitlines() if line.strip()]
    subject_lines: list[str] = []
    for line in lines[:20]:
        if re.search(r"(?:महाराष्ट्र\s+(?:शासन|िासन)|government\s+of\s+maharashtra)", line, re.IGNORECASE):
            break
        if len(line.strip()) >= 4:
            subject_lines.append(line.strip())
    return " ".join(subject_lines)[:1000], folder_department


def _legal_identity(text: str, kind: str) -> str:
    if kind not in {"ACT", "RULE"}:
        return ""
    for line in normalize_marathi_text(text).splitlines()[:100]:
        if LEGAL_MARKER_RE.search(line) and re.search(r"(?:19|20)[0-9]{2}", line):
            return normalize_legal_reference(line, kind)
    return ""


def identity_from_pages(
    item: LocalPdf,
    page_texts: list[tuple[int, str]],
    *,
    source_method: str,
    digital_quality: str,
    probe_pages: tuple[int, ...],
    ocr_pages: tuple[int, ...] = (),
    extraction_error: str = "",
    file_mtime_ns: int = 0,
) -> IdentityRecord:
    combined = "\n".join(text for _, text in page_texts)
    kind = detect_document_kind(combined)
    raw_number, number, source_page, ambiguous = extract_multiline_document_number(page_texts)
    legal_key = _legal_identity(combined, kind)
    if legal_key and not number:
        number = legal_key.casefold()
        raw_number = next((line for line in combined.splitlines() if LEGAL_MARKER_RE.search(line)), "")
        source_page = page_texts[0][0] if page_texts else None
    raw_date, normalized_date = _extract_date(page_texts, source_page)
    subject, raw_department = _extract_subject_and_department(page_texts, item.department)
    structurally_valid = bool(NUMBER_STRUCTURE_RE.search(number)) or bool(legal_key)
    if ambiguous:
        status, quality = "AMBIGUOUS_METADATA", "LOW"
    elif structurally_valid and source_page is not None:
        status = "IDENTIFIED_EXACT" if (kind != "UNKNOWN" or normalized_date or legal_key) else "IDENTIFIED_PARTIAL"
        quality = "HIGH" if status == "IDENTIFIED_EXACT" else "MEDIUM"
    elif number:
        status, quality = "IDENTIFIED_PARTIAL", "LOW"
    elif extraction_error:
        status, quality = "EXTRACTION_FAILED", "BLOCKED"
    elif digital_quality in BAD_DIGITAL_CLASSES:
        status, quality = ("QUALITY_BLOCKED", "BLOCKED") if source_method == "OCR" else ("NEEDS_OCR", "LOW")
    else:
        status, quality = "NO_IDENTIFIER", "LOW"
    return IdentityRecord(
        relative_path=item.relative_path,
        filename=item.file_name,
        department_folder=item.department,
        normalized_department=normalize_department_alias(item.department),
        sha256=item.sha256,
        file_size=item.file_size,
        file_mtime_ns=file_mtime_ns,
        page_count=item.page_count,
        filename_date=item.filename_date,
        document_kind=kind,
        raw_document_number=raw_number,
        normalized_document_number=number,
        raw_date=raw_date,
        normalized_date=normalized_date,
        raw_department=raw_department,
        raw_subject=subject,
        subject_preview=subject[:500],
        legal_canonical_key=legal_key,
        identity_source_page=source_page,
        identity_source_method=source_method if source_page else "NONE",
        identity_parse_status=status,
        identity_quality=quality,
        digital_quality=digital_quality,
        probe_pages=probe_pages,
        ocr_used=bool(ocr_pages),
        ocr_pages=ocr_pages,
        extraction_error=extraction_error,
    )


def _is_strong(record: IdentityRecord) -> bool:
    return record.identity_parse_status == "IDENTIFIED_EXACT" and record.identity_quality == "HIGH"


def probe_pdf_identity(
    root: Path,
    item: LocalPdf,
    *,
    digital_page_limit: int = 5,
    ocr_page_limit: int = 3,
    ocr_page_func: Callable[..., str] | None = None,
    tesseract_available_func: Callable[[], bool] | None = None,
) -> IdentityRecord:
    started = time.perf_counter()
    path = resolve_inside_root(root, root / item.relative_path)
    stat = path.stat()
    digital_texts: list[tuple[int, str]] = []
    probe_pages: list[int] = []
    qualities: list[str] = []
    try:
        reader = PdfReader(str(path), strict=False)
        page_total = min(len(reader.pages), max(1, digital_page_limit))
        record: IdentityRecord | None = None
        for index in range(page_total):
            page_number = index + 1
            try:
                text = normalize_marathi_text(reader.pages[index].extract_text() or "")
            except Exception:
                text = ""
            digital_texts.append((page_number, text))
            probe_pages.append(page_number)
            qualities.append(digital_text_classification(text))
            record = identity_from_pages(
                item,
                digital_texts,
                source_method="DIGITAL",
                digital_quality=max(qualities, key=lambda value: value in {"GOOD_MARATHI", "GOOD_ENGLISH", "VERY_SHORT_VALID"}),
                probe_pages=tuple(probe_pages),
                file_mtime_ns=stat.st_mtime_ns,
            )
            if _is_strong(record):
                return replace(record, elapsed_seconds=time.perf_counter() - started)

        assert record is not None
        poor_early_text = record.identity_parse_status == "NEEDS_OCR" or all(
            value in BAD_DIGITAL_CLASSES for value in qualities[: min(3, len(qualities))]
        )
        if not poor_early_text or record.normalized_document_number:
            return replace(record, elapsed_seconds=time.perf_counter() - started)

        available = (tesseract_available_func or _tesseract_available)()
        if not available:
            return replace(
                record,
                identity_parse_status="NEEDS_OCR",
                identity_quality="LOW",
                elapsed_seconds=time.perf_counter() - started,
            )

        import pypdfium2 as pdfium

        pdfium_doc = pdfium.PdfDocument(str(path))
        ocr_texts: list[tuple[int, str]] = []
        ocr_pages: list[int] = []
        try:
            for index in range(min(len(reader.pages), max(1, ocr_page_limit))):
                page_number = index + 1
                text = normalize_marathi_text((ocr_page_func or _ocr_page)(pdfium_doc, index, try_rotations=False))
                ocr_texts.append((page_number, text))
                ocr_pages.append(page_number)
                record = identity_from_pages(
                    item,
                    ocr_texts,
                    source_method="OCR",
                    digital_quality=";".join(qualities),
                    probe_pages=tuple(probe_pages),
                    ocr_pages=tuple(ocr_pages),
                    file_mtime_ns=stat.st_mtime_ns,
                )
                if _is_strong(record):
                    break
        finally:
            pdfium_doc.close()
        return replace(record, elapsed_seconds=time.perf_counter() - started)
    except Exception as error:
        fallback = identity_from_pages(
            item,
            digital_texts,
            source_method="DIGITAL",
            digital_quality=";".join(qualities),
            probe_pages=tuple(probe_pages),
            extraction_error=f"{type(error).__name__}:{error}",
            file_mtime_ns=stat.st_mtime_ns,
        )
        return replace(fallback, elapsed_seconds=time.perf_counter() - started)
