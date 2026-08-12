from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime


DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
SPACE_RE = re.compile(r"[\t\u00a0 ]+")
NUMBER_LABEL_RE = re.compile(
    r"^(?:(?:शासन\s+)?(?:निर्णय|परिपत्रक|पत्र|अधिसूचना|आदेश)\s*)?"
    r"(?:क्रमांक|क्र\.?|No\.?|Number|G\.?\s*R\.?\s*No\.?)\s*[:\-–—]*\s*",
    re.IGNORECASE,
)
MARATHI_MONTHS = {
    "जानेवारी": 1,
    "फेब्रुवारी": 2,
    "मार्च": 3,
    "एप्रिल": 4,
    "मे": 5,
    "जून": 6,
    "जुलै": 7,
    "जुले": 7,
    "ऑगस्ट": 8,
    "सप्टेंबर": 9,
    "ऑक्टोबर": 10,
    "ऑक्टोंबर": 10,
    "नोव्हेंबर": 11,
    "डिसेंबर": 12,
}
ENGLISH_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    if name
}
ENGLISH_MONTHS.update(
    {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedDate:
    raw: str
    iso_date: str
    status: str
    reason: str


def devanagari_to_ascii_digits(value: str) -> str:
    return value.translate(DEVANAGARI_DIGITS)


def normalize_reference_text(value: str) -> str:
    """Conservative Unicode/spacing normalization for diagnostic matching."""
    value = unicodedata.normalize("NFC", value or "")
    value = ZERO_WIDTH_RE.sub("", value)
    value = devanagari_to_ascii_digits(value)
    lines = [SPACE_RE.sub(" ", line).strip() for line in value.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_document_number(value: str) -> str:
    """Normalize an identifier for matching while callers retain the raw value."""
    value = normalize_reference_text(value)
    value = NUMBER_LABEL_RE.sub("", value).strip(" :;,.()[]{}\"'“”")
    value = re.sub(r"प्र\s*\.\s*क्र\s*\.?", "प्र.क्र.", value, flags=re.IGNORECASE)
    value = re.sub(r"C\s*\.\s*R\s*\.?", "C.R.", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<=\d)\s+(?=\d)", "", value)
    value = re.sub(r"\s*/\s*", "/", value)
    value = re.sub(r"[-‐‑‒–—]", "", value)
    value = re.sub(r"\s*\.\s*", ".", value)
    value = re.sub(r"[\s:ः,;()\[\]{}\"'“”]", "", value)
    return value.casefold().strip(".")


normalize_gr_number = normalize_document_number


def _valid_date(day: int, month: int, year: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_date(value: str) -> NormalizedDate:
    raw = (value or "").strip()
    text = normalize_reference_text(raw)
    if not text:
        return NormalizedDate(raw, "", "MISSING", "No reference date was extracted")

    numeric = re.search(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})(?!\d)", text)
    if numeric:
        iso = _valid_date(*(int(part) for part in numeric.groups()))
        if iso:
            return NormalizedDate(raw, iso, "PARSED_EXACT", "Numeric day/month/year")

    marathi_months = "|".join(map(re.escape, MARATHI_MONTHS))
    marathi = re.search(
        rf"(?<!\d)(\d{{1,2}})\s*(?:,|रोजी|दिनांक|[वल]?दि?\.)?\s*({marathi_months})\s*,?\s*(\d{{4}})(?!\d)",
        text,
        re.IGNORECASE,
    )
    if marathi:
        iso = _valid_date(int(marathi.group(1)), MARATHI_MONTHS[marathi.group(2)], int(marathi.group(3)))
        if iso:
            return NormalizedDate(raw, iso, "PARSED_TEXTUAL", "Marathi named month")

    english_months = "|".join(map(re.escape, ENGLISH_MONTHS))
    english = re.search(
        rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({english_months})\s*,?\s*(\d{{4}})(?!\d)",
        text,
        re.IGNORECASE,
    )
    if english:
        iso = _valid_date(
            int(english.group(1)),
            ENGLISH_MONTHS[english.group(2).casefold()],
            int(english.group(3)),
        )
        if iso:
            return NormalizedDate(raw, iso, "PARSED_TEXTUAL", "English named month")

    for fmt in ("%Y-%m-%d",):
        try:
            return NormalizedDate(raw, datetime.strptime(text, fmt).date().isoformat(), "PARSED_EXACT", "ISO date")
        except ValueError:
            pass
    return NormalizedDate(raw, "", "UNPARSEABLE", "No supported observed date format matched")
