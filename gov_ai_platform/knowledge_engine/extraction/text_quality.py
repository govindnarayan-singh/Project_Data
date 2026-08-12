from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
MARATHI_SIGNAL_WORDS = (
    "महाराष्ट्र शासन", "शासन निर्णय", "विभाग", "दिनांक", "विषय", "संदर्भ",
)
_WORD_INITIAL_MARK_RE = re.compile(r"(?<![\u0900-\u097f])[\u093a-\u094d\u0951-\u0957]")
_KNOWN_BROKEN_WORD_RE = re.compile(r"(?:महािाष्ट्र|रवभाग|रदनांक|रनण[ट्ण]|सरचव|कार्?यपद्धती)")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_LATIN_VOWEL_RE = re.compile(r"[AEIOUYaeiouy]")
_REPEATED_LATIN_RE = re.compile(r"([A-Za-z])\1\1", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def devanagari_to_ascii_digits(text: str) -> str:
    return text.translate(DEVANAGARI_DIGITS)


def normalize_marathi_text(text: str) -> str:
    """Conservative normalization preserving Devanagari and line boundaries."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned: list[str] = []
    previous_blank = False
    for line in text.split("\n"):
        line = re.sub(r"[\t\u00A0 ]+", " ", line).strip()
        is_blank = not line
        if is_blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = is_blank
    return "\n".join(cleaned).strip()


def devanagari_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    return 0.0 if not letters else sum("\u0900" <= char <= "\u097F" for char in letters) / len(letters)


def latin_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    return 0.0 if not letters else sum(unicodedata.name(char, "").startswith("LATIN ") for char in letters) / len(letters)


def malformed_devanagari_count(text: str) -> int:
    normalized = normalize_marathi_text(text)
    return len(_WORD_INITIAL_MARK_RE.findall(normalized)) + 2 * len(_KNOWN_BROKEN_WORD_RE.findall(normalized))


def _latin_word_signals(text: str) -> tuple[int, float, float]:
    words = _LATIN_WORD_RE.findall(text)
    if not words:
        return 0, 0.0, 0.0
    single_share = sum(len(word) == 1 for word in words) / len(words)
    plausible_share = sum(
        len(word) >= 2 and _LATIN_VOWEL_RE.search(word) is not None and _REPEATED_LATIN_RE.search(word) is None
        for word in words
    ) / len(words)
    return len(words), single_share, plausible_share


def digital_text_classification(text: str) -> str:
    text = normalize_marathi_text(text)
    if sum(char.isalnum() for char in text) < 4:
        return "EMPTY_OR_IMAGE_ONLY"
    malformed = malformed_devanagari_count(text)
    if malformed >= 2:
        return "CORRUPTED_MARATHI"
    dev_ratio = devanagari_ratio(text)
    lat_ratio = latin_ratio(text)
    word_count, single_share, plausible_share = _latin_word_signals(text)
    if lat_ratio >= 0.65 and word_count >= 20 and (single_share >= 0.35 or plausible_share < 0.45):
        return "SUSPICIOUS_GARBLED_LATIN"
    if len(text) < 80:
        short_latin_valid = lat_ratio >= 0.50 and word_count >= 1 and single_share <= 0.25 and plausible_share >= 0.60
        short_marathi_valid = dev_ratio >= 0.50 and malformed == 0
        return "VERY_SHORT_VALID" if short_latin_valid or short_marathi_valid else "UNCERTAIN"
    if dev_ratio >= 0.55:
        return "GOOD_MARATHI"
    coherent_latin = lat_ratio >= 0.65 and word_count >= 3 and single_share <= 0.25 and plausible_share >= 0.60
    if coherent_latin and dev_ratio < 0.08:
        return "GOOD_ENGLISH"
    return "UNCERTAIN"


def extracted_text_quality(text: str) -> float:
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
    signal_score = min(sum(word in text for word in MARATHI_SIGNAL_WORDS) / 3.0, 1.0)
    score = 0.35 * length_score + 0.45 * max(dev_score, latin_score) + 0.20 * signal_score
    if malformed_devanagari_count(text) >= 2:
        return min(score, 0.45)
    return min(score, 0.54) if classification == "UNCERTAIN" else score


def should_ocr(text: str) -> bool:
    return digital_text_classification(text) not in {"GOOD_MARATHI", "GOOD_ENGLISH", "VERY_SHORT_VALID"}
