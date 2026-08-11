from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
MARATHI_SIGNAL_WORDS = (
    "महाराष्ट्र शासन",
    "शासन निर्णय",
    "विभाग",
    "दिनांक",
    "विषय",
    "संदर्भ",
)

# Dependent vowel signs cannot begin a Devanagari word.  A surprisingly common
# failure mode in old Marathi PDFs is a broken ToUnicode map which emits the
# pre-base sign before the consonant (for example "िवभाग"/"रवभाग").
_WORD_INITIAL_MARK_RE = re.compile(r"(?<![\u0900-\u097f])[\u093a-\u094d\u0951-\u0957]")
_KNOWN_BROKEN_WORD_RE = re.compile(
    r"(?:महािाष्ट्र|रवभाग|रदनांक|रनण[ट्ण]|सरचव|कार्?यपद्धती)"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def devanagari_to_ascii_digits(text: str) -> str:
    return text.translate(DEVANAGARI_DIGITS)


def normalize_marathi_text(text: str) -> str:
    """Conservative normalization: preserve Devanagari content and line boundaries."""
    if not text:
        return ""

    text = unicodedata.normalize("NFC", text)
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    cleaned_lines: list[str] = []
    previous_blank = False
    for line in text.split("\n"):
        line = re.sub(r"[\t\u00A0 ]+", " ", line).strip()
        is_blank = not line
        if is_blank and previous_blank:
            continue
        cleaned_lines.append(line)
        previous_blank = is_blank

    return "\n".join(cleaned_lines).strip()


def devanagari_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    dev = sum(1 for c in letters if "\u0900" <= c <= "\u097F")
    return dev / len(letters)


def malformed_devanagari_count(text: str) -> int:
    """Return strong indicators of bad PDF Unicode mapping, not spelling errors."""
    normalized = normalize_marathi_text(text)
    return len(_WORD_INITIAL_MARK_RE.findall(normalized)) + 2 * len(
        _KNOWN_BROKEN_WORD_RE.findall(normalized)
    )


def extracted_text_quality(text: str) -> float:
    """
    Rough 0..1 signal used only to decide if OCR fallback is needed.
    It is NOT an OCR confidence score.
    """
    text = normalize_marathi_text(text)
    if not text:
        return 0.0

    length_score = min(len(text) / 500.0, 1.0)
    dev_score = min(devanagari_ratio(text) / 0.55, 1.0)
    signal_hits = sum(1 for word in MARATHI_SIGNAL_WORDS if word in text)
    signal_score = min(signal_hits / 3.0, 1.0)

    base_score = 0.35 * length_score + 0.45 * dev_score + 0.20 * signal_score
    # Cap text with strong structural corruption below the automatic OCR cutoff.
    malformed = malformed_devanagari_count(text)
    if malformed >= 2:
        return min(base_score, 0.45)
    return base_score


def should_ocr(text: str) -> bool:
    text = normalize_marathi_text(text)
    if len(text) < 80:
        return True

    # A Marathi GR should normally contain meaningful Devanagari. This also catches
    # many legacy/custom-font PDFs where text extraction returns Latin-like garbage.
    if devanagari_ratio(text) < 0.08 and not any(w in text for w in MARATHI_SIGNAL_WORDS):
        return True

    if malformed_devanagari_count(text) >= 2:
        return True

    return extracted_text_quality(text) < 0.55
