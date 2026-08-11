from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .text_utils import devanagari_to_ascii_digits, normalize_marathi_text


MONTHS_MR = {
    "जानेवारी": 1,
    "फेब्रुवारी": 2,
    "मार्च": 3,
    "एप्रिल": 4,
    "मे": 5,
    "जून": 6,
    "जुलै": 7,
    "ऑगस्ट": 8,
    "सप्टेंबर": 9,
    "ऑक्टोबर": 10,
    "नोव्हेंबर": 11,
    "डिसेंबर": 12,
}

STOP_HEADINGS = (
    "संदर्भ",
    "प्रस्तावना",
    "शासन निर्णय",
    "निर्णय",
    "आदेश",
    "परिपत्रक",
)


def _lines(text: str) -> list[str]:
    return [line.strip(" :-\t") for line in normalize_marathi_text(text).splitlines() if line.strip()]


def detect_document_type(text: str) -> str | None:
    head = text[:5000]
    for label in (
        "शासन निर्णय",
        "शासन परिपत्रक",
        "अधिसूचना",
        "परिपत्रक",
        "शुद्धिपत्रक",
    ):
        if label in head:
            return label
    return None


def extract_gr_number(text: str) -> str | None:
    lines = _lines(text[:8000])
    patterns = (
        r"(?:शासन\s+निर्णय\s+)?क्रमांक\s*[:\-]?\s*(.+)",
        r"(?:शा\.?\s*नि\.?)\s*क्रमांक\s*[:\-]?\s*(.+)",
        r"क्र(?:मांक)?\.?\s*[:\-]?\s*(.+)",
    )
    for line in lines:
        for pattern in patterns:
            m = re.search(pattern, line, flags=re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if 3 <= len(value) <= 220:
                    return devanagari_to_ascii_digits(value)
    return None


def extract_subject(text: str) -> str | None:
    lines = _lines(text[:12000])
    for i, line in enumerate(lines):
        if re.match(r"^विषय\s*[:\-]?", line):
            value = re.sub(r"^विषय\s*[:\-]?\s*", "", line).strip()
            collected = [value] if value else []
            for nxt in lines[i + 1 : i + 5]:
                if any(nxt.startswith(h) for h in STOP_HEADINGS):
                    break
                collected.append(nxt)
            subject = " ".join(x for x in collected if x).strip()
            return subject or None

    # Many Maharashtra GRs put an unlabelled, wrapped title at the very top of
    # page 1, immediately before "महाराष्ट्र शासन".
    title_lines: list[str] = []
    for line in lines[:12]:
        if "महाराष्ट्र शासन" in line:
            break
        if re.match(r"^(?:-{2,}\s*)?PAGE\s+\d+(?:\s*-{2,})?$", line, re.IGNORECASE):
            continue
        if any(
            marker in line
            for marker in ("शासन निर्णय क्रमांक", "क्रमांक", "दिनांक", "संदर्भ")
        ):
            continue
        title_lines.append(line)
    subject = " ".join(title_lines).strip()
    return subject or None
    return None


def extract_references(text: str) -> str | None:
    lines = _lines(text[:18000])
    for i, line in enumerate(lines):
        if re.match(r"^संदर्भ\s*[:\-]?", line):
            first = re.sub(r"^संदर्भ\s*[:\-]?\s*", "", line).strip()
            collected = [first] if first else []
            for nxt in lines[i + 1 : i + 12]:
                if any(nxt.startswith(h) for h in ("प्रस्तावना", "शासन निर्णय", "आदेश")):
                    break
                collected.append(nxt)
            value = "\n".join(x for x in collected if x).strip()
            return value or None
    return None


def _valid_date(day: int, month: int, year: int) -> date | None:
    try:
        if 1900 <= year <= 2100:
            return date(year, month, day)
    except ValueError:
        pass
    return None


def extract_date(text: str) -> date | None:
    source = devanagari_to_ascii_digits(text[:10000])

    # 12/03/2024, 12-03-2024, 12.03.2024
    numeric = re.search(
        r"(?:दिनांक|दि\.)\s*[:\-]?\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})",
        source,
    )
    if numeric:
        d = _valid_date(int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3)))
        if d:
            return d

    # 12 मार्च 2024
    month_names = "|".join(map(re.escape, MONTHS_MR))
    named = re.search(
        rf"(?:दिनांक|दि\.)\s*[:\-]?\s*(\d{{1,2}})\s+({month_names})[,\s]+(\d{{4}})",
        source,
    )
    if named:
        return _valid_date(
            int(named.group(1)), MONTHS_MR[named.group(2)], int(named.group(3))
        )

    return None


def date_from_filename(file_name: str | None) -> date | None:
    """Use an initial YYYYMMDD filename date only when document text has none."""
    if not file_name:
        return None
    match = re.match(r"^(\d{4})(\d{2})(\d{2})", Path(file_name).stem)
    if not match:
        return None
    return _valid_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))


def extract_metadata(
    text: str, department_from_folder: str, file_name: str | None = None
) -> dict:
    document_date = extract_date(text)
    return {
        "department": department_from_folder,
        "document_type": detect_document_type(text),
        "gr_number": extract_gr_number(text),
        "gr_date": document_date or date_from_filename(file_name),
        "subject": extract_subject(text),
        "references_text": extract_references(text),
    }
