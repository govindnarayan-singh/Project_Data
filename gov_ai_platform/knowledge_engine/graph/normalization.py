from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime


DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
SPACE_RE = re.compile(r"[\t\u00a0 ]+")
NUMBER_LABEL_RE = re.compile(
    r"^(?:(?:शासन\s+)?(?:निर्णय|परिपत्रक|पत्र|अधिसूचना|आदेश|शुद्धीपत्रक)\s*)?"
    r"(?:क्रमांक|क्र\.?|No\.?|Number|G\.?\s*R\.?\s*No\.?)\s*[:\-–—]*\s*",
    re.IGNORECASE,
)
MARATHI_MONTHS = {
    "जानेवारी": 1, "फेब्रुवारी": 2, "मार्च": 3, "एप्रिल": 4, "मे": 5,
    "जून": 6, "जुलै": 7, "जुले": 7, "ऑगस्ट": 8, "सप्टेंबर": 9,
    "ऑक्टोबर": 10, "ऑक्टोंबर": 10, "नोव्हेंबर": 11, "डिसेंबर": 12,
}
ENGLISH_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"))
    if name
}
ENGLISH_MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})


@dataclass(frozen=True, slots=True)
class NormalizedDate:
    raw: str
    value: date | None
    status: str
    reason: str

    @property
    def iso_date(self) -> str:
        return self.value.isoformat() if self.value else ""


def normalize_unicode(value: str) -> str:
    return ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFC", value or ""))


def normalize_digits(value: str) -> str:
    return normalize_unicode(value).translate(DEVANAGARI_DIGITS)


def normalize_reference_whitespace(value: str) -> str:
    lines = [SPACE_RE.sub(" ", line).strip() for line in normalize_digits(value).replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_document_number(value: str) -> str:
    """Return a matching key while leaving the caller's raw value untouched."""
    text = normalize_reference_whitespace(value)
    text = NUMBER_LABEL_RE.sub("", text).strip(" :;,.()[]{}\"'“”")
    text = re.sub(r"प्र\s*\.\s*क्र\s*\.?", "प्र.क्र.", text, flags=re.IGNORECASE)
    text = re.sub(r"C\s*\.\s*R\s*\.?", "C.R.", text, flags=re.IGNORECASE)
    text = re.sub(r"[‐‑‒–—]", "-", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"[\s:ः,;()\[\]{}\"'“”]", "", text)
    # Observed corpus variants omit or insert the separator between a Marathi/
    # Latin prefix and a four-digit series year. Canonical matching retains one.
    text = re.sub(r"(?<=[A-Za-z\u0900-\u097f])(?=(?:19|20)\d{2}/)", "-", text)
    return text.casefold().strip(".-")


normalize_gr_number = normalize_document_number


def normalize_department_alias(value: str) -> str:
    text = normalize_reference_whitespace(value).casefold()
    text = re.sub(r"\b(the|government of maharashtra|maharashtra government)\b", "", text)
    text = re.sub(r"[^0-9a-z\u0900-\u097f]+", " ", text)
    return " ".join(text.split())


def _valid_date(day: int, month: int, year: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_reference_date(value: str) -> NormalizedDate:
    raw = (value or "").strip()
    text = normalize_reference_whitespace(raw)
    if not text:
        return NormalizedDate(raw, None, "MISSING", "No reference date extracted")
    numeric = re.search(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})(?!\d)", text)
    if numeric:
        parsed = _valid_date(*(int(part) for part in numeric.groups()))
        if parsed:
            return NormalizedDate(raw, parsed, "PARSED_EXACT", "Numeric day/month/year")
        return NormalizedDate(raw, None, "INVALID", "Numeric date is not calendar-valid")
    marathi_months = "|".join(map(re.escape, MARATHI_MONTHS))
    marathi = re.search(rf"(?<!\d)(\d{{1,2}})\s*(?:रोजी)?\s*({marathi_months})\s*,?\s*(\d{{4}})(?!\d)", text, re.IGNORECASE)
    if marathi:
        parsed = _valid_date(int(marathi.group(1)), MARATHI_MONTHS[marathi.group(2)], int(marathi.group(3)))
        if parsed:
            return NormalizedDate(raw, parsed, "PARSED_TEXTUAL", "Marathi named month")
    english_months = "|".join(map(re.escape, ENGLISH_MONTHS))
    english = re.search(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({english_months})\s*,?\s*(\d{{4}})(?!\d)", text, re.IGNORECASE)
    if english:
        parsed = _valid_date(int(english.group(1)), ENGLISH_MONTHS[english.group(2).casefold()], int(english.group(3)))
        if parsed:
            return NormalizedDate(raw, parsed, "PARSED_TEXTUAL", "English named month")
    try:
        return NormalizedDate(raw, datetime.strptime(text, "%Y-%m-%d").date(), "PARSED_EXACT", "ISO date")
    except ValueError:
        return NormalizedDate(raw, None, "UNPARSEABLE", "No observed date format matched")


LEGAL_KIND_RE = re.compile(r"(?:अधिनियम|नियमावली|\bact\b|\brules?\b)", re.IGNORECASE)


def normalize_legal_reference(value: str, kind: str) -> str:
    """Canonicalize observed Act/Rule titles instead of treating them as GR numbers."""
    text = normalize_reference_whitespace(value)
    year_match = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)
    year = year_match.group(0) if year_match else ""
    if kind == "ACT":
        known = (
            (r"rights?\s+of\s+persons?\s+with\s+disabilities|दिव्यांग\s+व्यक्?ती\s+हक्क", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT"),
            (r"environment\s*\(protection\)\s*act", "ENVIRONMENT_PROTECTION_ACT"),
            (r"maharashtra\s+zilla\s+parishads?.*panchayat\s+samiti", "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_ACT"),
            (r"महाराष्ट्र\s+जिल्हा\s+परिषदा.*पंचायत\s+समित", "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_ACT"),
        )
    else:
        known = (
            (r"construction.*demolition\s+waste\s+management\s+rules", "CONSTRUCTION_AND_DEMOLITION_WASTE_MANAGEMENT_RULES"),
            (r"development\s+control.*(?:rules|regulations)|विकास\s+नियंत्रण\s+नियमावली", "DEVELOPMENT_CONTROL_REGULATIONS"),
        )
    for pattern, canonical in known:
        if re.search(pattern, text, re.IGNORECASE):
            return f"{canonical}_{year}" if year else canonical
    before_kind = LEGAL_KIND_RE.split(text, maxsplit=1)[0]
    base = before_kind if len(before_kind.strip()) >= 4 else text
    base = re.sub(r"[^0-9A-Za-z\u0900-\u097f]+", "_", base).strip("_").upper()
    suffix = "ACT" if kind == "ACT" else "RULE"
    if suffix not in base:
        base = f"{base}_{suffix}" if base else suffix
    return f"{base}_{year}" if year and not base.endswith(year) else base


# Backward-compatible diagnostic names.
normalize_reference_text = normalize_reference_whitespace
normalize_date = normalize_reference_date
devanagari_to_ascii_digits = normalize_digits
