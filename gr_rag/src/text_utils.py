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


def latin_ratio(text: str) -> float:
    """Return the share of alphabetic characters that belong to the Latin script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(unicodedata.name(c, "").startswith("LATIN ") for c in letters)
    return latin / len(letters)


def malformed_devanagari_count(text: str) -> int:
    """Return strong indicators of bad PDF Unicode mapping, not spelling errors."""
    normalized = normalize_marathi_text(text)
    return len(_WORD_INITIAL_MARK_RE.findall(normalized)) + 2 * len(
        _KNOWN_BROKEN_WORD_RE.findall(normalized)
    )


_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_LATIN_VOWEL_RE = re.compile(r"[AEIOUYaeiouy]")
_REPEATED_LATIN_RE = re.compile(r"([A-Za-z])\1\1", re.IGNORECASE)


def _latin_word_signals(text: str) -> tuple[int, float, float]:
    """Return token count, single-letter share, and plausible-word share."""
    words = _LATIN_WORD_RE.findall(text)
    if not words:
        return 0, 0.0, 0.0

    single_share = sum(len(word) == 1 for word in words) / len(words)
    plausible_share = sum(
        len(word) >= 2
        and _LATIN_VOWEL_RE.search(word) is not None
        and _REPEATED_LATIN_RE.search(word) is None
        for word in words
    ) / len(words)
    return len(words), single_share, plausible_share


def digital_text_classification(text: str) -> str:
    """Classify a page's direct text layer using deterministic script signals."""
    text = normalize_marathi_text(text)
    alphanumeric_count = sum(c.isalnum() for c in text)
    if alphanumeric_count < 4:
        return "EMPTY_OR_IMAGE_ONLY"

    malformed = malformed_devanagari_count(text)
    if malformed >= 2:
        return "CORRUPTED_MARATHI"

    dev_ratio = devanagari_ratio(text)
    lat_ratio = latin_ratio(text)
    word_count, single_share, plausible_share = _latin_word_signals(text)

    # Bad hidden OCR/ToUnicode layers commonly consist of hundreds of isolated
    # Latin characters and fragments. Normal English prose and place-name lists
    # have far fewer one-character tokens and a much higher plausible-word share.
    suspicious_latin = (
        lat_ratio >= 0.65
        and word_count >= 20
        and (single_share >= 0.35 or plausible_share < 0.45)
    )
    if suspicious_latin:
        return "SUSPICIOUS_GARBLED_LATIN"

    if len(text) < 80:
        short_latin_valid = (
            lat_ratio >= 0.50
            and word_count >= 1
            and single_share <= 0.25
            and plausible_share >= 0.60
        )
        short_marathi_valid = dev_ratio >= 0.50 and malformed == 0
        if short_latin_valid or short_marathi_valid:
            return "VERY_SHORT_VALID"
        return "UNCERTAIN"

    if dev_ratio >= 0.55:
        return "GOOD_MARATHI"

    coherent_latin = (
        lat_ratio >= 0.65
        and word_count >= 3
        and single_share <= 0.25
        and plausible_share >= 0.60
    )
    if coherent_latin and dev_ratio < 0.08:
        return "GOOD_ENGLISH"

    return "UNCERTAIN"


def extracted_text_quality(text: str) -> float:
    """
    Rough 0..1 signal used only to decide if OCR fallback is needed.
    It is NOT an OCR confidence score.
    """
    text = normalize_marathi_text(text)
    if not text:
        return 0.0

    classification = digital_text_classification(text)
    if classification == "EMPTY_OR_IMAGE_ONLY":
        return 0.0
    if classification == "VERY_SHORT_VALID":
        return 0.60
    if classification == "SUSPICIOUS_GARBLED_LATIN":
        return 0.20

    length_score = min(len(text) / 500.0, 1.0)
    dev_score = min(devanagari_ratio(text) / 0.55, 1.0)
    latin_score = min(latin_ratio(text) / 0.65, 1.0)
    signal_hits = sum(1 for word in MARATHI_SIGNAL_WORDS if word in text)
    signal_score = min(signal_hits / 3.0, 1.0)

    language_score = max(dev_score, latin_score)
    base_score = 0.35 * length_score + 0.45 * language_score + 0.20 * signal_score
    # Cap text with strong structural corruption below the automatic OCR cutoff.
    malformed = malformed_devanagari_count(text)
    if malformed >= 2:
        return min(base_score, 0.45)
    if classification == "UNCERTAIN":
        return min(base_score, 0.54)
    return base_score


def should_ocr(text: str) -> bool:
    classification = digital_text_classification(text)
    return classification not in {
        "GOOD_MARATHI",
        "GOOD_ENGLISH",
        "VERY_SHORT_VALID",
    }
