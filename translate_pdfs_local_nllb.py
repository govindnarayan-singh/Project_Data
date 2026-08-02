"""
Local Marathi PDF -> English PDF translator.

Open-source pipeline:
1. Render each PDF page as an image with PyMuPDF.
2. Recover proper Unicode Marathi with Tesseract OCR.
   This is required because many Maharashtra GR PDFs use legacy embedded fonts
   whose extracted text is corrupted even though the PDF is visually clear.
3. Translate Marathi -> English locally with Meta NLLB-200 distilled 600M.
4. Save OCR/translation caches.
5. Generate an English PDF using Chromium through Playwright.
6. Resume automatically:
   - existing *_English.pdf outputs are skipped;
   - failed PDFs can be retried with RUN_MODE="failed_only" or "resume";
   - completed OCR and page translations are cached;
   - difficult units are recovered individually and written to a review CSV.

No Google/Gemini API key is used.
"""

from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

import fitz
import pytesseract
import torch
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from playwright.sync_api import sync_playwright
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_FOLDER = Path(
    "GRs/Environment Department"
)

# Keep this identical to the Gemini output directory.
# The script will automatically skip the 50 PDFs already translated there.
OUTPUT_FOLDER = Path(
    "translated/Environment Department"
)

HTML_FOLDER = OUTPUT_FOLDER / "html_local"
TEXT_FOLDER = OUTPUT_FOLDER / "text_local"

CACHE_ROOT = Path("cache_local")
OCR_CACHE_FOLDER = CACHE_ROOT / "ocr"
TRANSLATION_CACHE_FOLDER = CACHE_ROOT / "translations"
FAILED_FOLDER = CACHE_ROOT / "failed"
REVIEW_FOLDER = OUTPUT_FOLDER / "review_local"

LOG_FOLDER = Path("logs")
CSV_LOG = LOG_FOLDER / "local_translation_results.csv"
TEXT_LOG = LOG_FOLDER / "local_translation_runtime.log"

for folder in (
    OUTPUT_FOLDER,
    HTML_FOLDER,
    TEXT_FOLDER,
    OCR_CACHE_FOLDER,
    TRANSLATION_CACHE_FOLDER,
    FAILED_FOLDER,
    REVIEW_FOLDER,
    LOG_FOLDER,
):
    folder.mkdir(parents=True, exist_ok=True)

# 0 means process every remaining PDF.
# For the first test, set this to 1.
MAX_NEW_FILES = 0

# Existing English PDFs are always skipped unless this is True.
FORCE_RETRANSLATE = False

# Processing mode:
# "failed_only" -> retry only PDFs having a failure marker.
# "resume"      -> skip completed PDFs, retry failed PDFs, then process new PDFs.
# "new_only"    -> skip completed and previously failed PDFs.
RUN_MODE = "resume"

# Do not retry the same genuinely broken PDF forever.
MAX_PDF_FAILURE_ATTEMPTS = 3

# Delete a failed marker automatically after successful or review output.
REMOVE_FAILED_MARKER_AFTER_SUCCESS = True

# OCR configuration.
TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OCR_LANGUAGE = "mar+eng"
OCR_DPI = 300
OCR_PSM = 3
MIN_OCR_CHARACTERS_PER_PAGE = 25
MIN_OCR_DEVANAGARI_RATIO = 0.08

# Local translation model.
MODEL_NAME = "facebook/nllb-200-distilled-600M"
SOURCE_LANGUAGE = "mar_Deva"
TARGET_LANGUAGE = "eng_Latn"

# NLLB was trained with limited sequence lengths. Keep input safely below 512.
MAX_SOURCE_TOKENS = 380
MAX_NEW_TOKENS = 512
NUM_BEAMS = 4

# Conservative batches for a GTX 1650 Ti with 4 GB VRAM.
GPU_BATCH_SIZE = 4
CPU_BATCH_SIZE = 2

# Translation validation and recovery.
# Short words such as "वाचा" -> "Read" are allowed to produce short English.
MAX_OUTPUT_DEVANAGARI_RATIO = 0.08
LONG_SOURCE_VALIDATION_MIN_CHARS = 80
MIN_LONG_TRANSLATION_LENGTH_RATIO = 0.04
UNIT_RECOVERY_ATTEMPTS = 2

# When recovery still cannot produce a fully clean translation, create the PDF
# and a review CSV instead of losing the entire document.
ALLOW_REVIEW_OUTPUT = True

# A researcher can type a correction into the corrected_english column of a
# review CSV. The next rebuild imports that correction into the cache.
APPLY_MANUAL_REVIEW_CORRECTIONS = True

EMPTY_TRANSLATION_PLACEHOLDER = (
    "[Translation unavailable; check the review report and source PDF.]"
)

# Table-like OCR blocks are translated line by line to preserve rough structure.
TABLE_LINE_TRANSLATION = True

# First model run downloads weights. Set to True only after the model is cached.
OFFLINE_MODE = False

# Set the Hugging Face model cache to D: if desired.
# Example:
# os.environ["HF_HOME"] = r"D:\huggingface_cache"

if OFFLINE_MODE:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE

logging.basicConfig(
    filename=TEXT_LOG,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class TranslationUnit:
    page_number: int
    unit_number: int
    source_text: str
    translated_text: str = ""
    unit_type: str = "paragraph"  # paragraph | table_line | literal
    quality_status: str = "PENDING"  # PENDING | PASS | REVIEW
    quality_note: str = ""


@dataclass
class PageResult:
    page_number: int
    ocr_text: str
    units: list[TranslationUnit]


# ============================================================
# GENERAL HELPERS
# ============================================================

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def devanagari_ratio(text: str) -> float:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    count = sum("\u0900" <= character <= "\u097f" for character in letters)
    return count / len(letters)


def devanagari_letter_count(text: str) -> int:
    return sum(
        "\u0900" <= character <= "\u097f" and character.isalpha()
        for character in text
    )


def normalize_spaces(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_ocr_text(text: str) -> str:
    text = text.translate(DEVANAGARI_DIGITS)
    text = text.replace("|", " | ")
    text = normalize_spaces(text)

    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Remove lines consisting almost entirely of punctuation/noise.
        alphanumeric = sum(character.isalnum() for character in line)
        if line and alphanumeric == 0 and len(line) > 3:
            continue

        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def get_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as document:
        return document.page_count


def failed_marker_path(pdf_path: Path) -> Path:
    return FAILED_FOLDER / f"{pdf_path.stem}.json"


def output_pdf_path(pdf_path: Path) -> Path:
    return OUTPUT_FOLDER / f"{pdf_path.stem}_English.pdf"


def output_html_path(pdf_path: Path) -> Path:
    return HTML_FOLDER / f"{pdf_path.stem}_English.html"


def output_text_path(pdf_path: Path) -> Path:
    return TEXT_FOLDER / f"{pdf_path.stem}_English.txt"


def ocr_cache_path(pdf_path: Path) -> Path:
    return OCR_CACHE_FOLDER / f"{pdf_path.stem}.json"


def translation_cache_path(pdf_path: Path) -> Path:
    return TRANSLATION_CACHE_FOLDER / f"{pdf_path.stem}.json"


def review_report_path(pdf_path: Path) -> Path:
    return REVIEW_FOLDER / f"{pdf_path.stem}_review.csv"


def append_csv_log(
    filename: str,
    status: str,
    pages: int,
    seconds: float,
    device: str,
    message: str = "",
) -> None:
    exists = CSV_LOG.exists()
    with CSV_LOG.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(
                ["filename", "status", "pages", "seconds", "device", "message"]
            )
        writer.writerow(
            [filename, status, pages, f"{seconds:.2f}", device, message]
        )


# ============================================================
# OCR
# ============================================================

def verify_tesseract() -> None:
    executable = Path(TESSERACT_EXE)
    if not executable.exists():
        raise FileNotFoundError(
            "Tesseract was not found at:\n"
            f"{TESSERACT_EXE}\n"
            "Install Tesseract or change TESSERACT_EXE in the script."
        )

    languages = set(pytesseract.get_languages(config=""))
    missing: list[str] = []

    for language in OCR_LANGUAGE.split("+"):
        if language not in languages:
            missing.append(language)

    if missing:
        raise RuntimeError(
            "Tesseract language data is missing: "
            + ", ".join(missing)
            + "\nRun: tesseract --list-langs\n"
              "Make sure mar.traineddata and eng.traineddata are inside tessdata."
        )


def render_page(page: fitz.Page, dpi: int = OCR_DPI) -> Image.Image:
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)

    image = Image.frombytes(
        "RGB",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )
    return image


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    # Printed GR documents usually OCR better after grayscale + mild sharpening.
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=1)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.25)
    grayscale = grayscale.filter(ImageFilter.SHARPEN)
    return grayscale


def ocr_page(image: Image.Image, psm: int = OCR_PSM) -> str:
    config = (
        f"--oem 1 --psm {psm} "
        "-c preserve_interword_spaces=1"
    )

    text = pytesseract.image_to_string(
        image,
        lang=OCR_LANGUAGE,
        config=config,
    )
    return clean_ocr_text(text)


def ocr_candidate_score(text: str) -> float:
    """Prefer substantial OCR containing Marathi, but allow English/code pages."""
    compact = re.sub(r"\s+", "", text)
    return (
        len(compact)
        + 3.0 * devanagari_letter_count(text)
        + 0.5 * sum(character.isdigit() for character in text)
    )


def ocr_page_with_recovery(
    image: Image.Image,
    page_number: int,
) -> str:
    """
    Try several page-segmentation modes and keep the strongest OCR result.

    A page with little Marathi is not automatically an error: some GR pages
    contain English, reference numbers, URLs, signatures, or blank areas.
    """
    candidates: list[tuple[int, str]] = []

    for psm in dict.fromkeys((OCR_PSM, 6, 11)):
        text = ocr_page(image, psm=psm)
        candidates.append((psm, text))

    best_psm, best_text = max(
        candidates,
        key=lambda item: ocr_candidate_score(item[1]),
    )

    compact = re.sub(r"\s+", "", best_text)
    ratio = devanagari_ratio(best_text)

    if len(compact) < MIN_OCR_CHARACTERS_PER_PAGE:
        logging.warning(
            "Page %s has only %s OCR characters; accepting as a sparse/blank page "
            "(best PSM=%s).",
            page_number,
            len(compact),
            best_psm,
        )

    if ratio < MIN_OCR_DEVANAGARI_RATIO:
        logging.warning(
            "Page %s has low Marathi OCR ratio %.1f%%; accepting because the "
            "page may contain English, codes, or a signature page (best PSM=%s).",
            page_number,
            ratio * 100,
            best_psm,
        )

    return best_text


def extract_or_load_ocr(pdf_path: Path) -> list[str]:
    cache = ocr_cache_path(pdf_path)
    pages_text: list[str] = []

    with fitz.open(pdf_path) as document:
        total = document.page_count

        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            cached_pages = [
                str(page) for page in payload.get("pages", [])
            ]

            # Ignore an invalid cache containing more pages than the source.
            if len(cached_pages) <= total:
                pages_text = cached_pages

            if len(pages_text) == total:
                print(f"  Loaded complete OCR cache: {cache}")
                return pages_text

            if pages_text:
                print(
                    f"  Resuming OCR cache at page "
                    f"{len(pages_text) + 1}/{total}: {cache}"
                )

        for index in range(len(pages_text), total):
            page = document[index]
            page_number = index + 1
            print(f"  OCR page {page_number}/{total}")

            image = preprocess_for_ocr(render_page(page))
            text = ocr_page_with_recovery(image, page_number)
            pages_text.append(text)

            # Save after every page so an interruption does not lose prior OCR.
            atomic_write_json(
                cache,
                {
                    "source_pdf": pdf_path.name,
                    "completed_pages": len(pages_text),
                    "total_pages": total,
                    "pages": pages_text,
                },
            )

    return pages_text


# ============================================================
# DOCUMENT SEGMENTATION
# ============================================================

def is_table_like_block(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False

    signals = 0

    for line in lines:
        has_number = bool(re.search(r"\d", line))
        has_separator = " | " in line or bool(re.search(r"\s{2,}", line))
        shortish = len(line) <= 100

        if shortish and (has_number or has_separator):
            signals += 1

    return signals / len(lines) >= 0.45


def page_to_units(page_text: str, page_number: int) -> list[TranslationUnit]:
    raw_blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n+", page_text)
        if block.strip()
    ]

    units: list[TranslationUnit] = []
    unit_number = 0

    for block in raw_blocks:
        lines = [
            normalize_spaces(line)
            for line in block.splitlines()
            if normalize_spaces(line)
        ]

        if not lines:
            continue

        if TABLE_LINE_TRANSLATION and is_table_like_block(lines):
            for line in lines:
                unit_number += 1
                units.append(
                    TranslationUnit(
                        page_number=page_number,
                        unit_number=unit_number,
                        source_text=line,
                        unit_type="table_line",
                    )
                )
        else:
            paragraph = normalize_spaces(" ".join(lines))
            unit_number += 1
            units.append(
                TranslationUnit(
                    page_number=page_number,
                    unit_number=unit_number,
                    source_text=paragraph,
                    unit_type="paragraph",
                )
            )

    if not units and page_text.strip():
        units.append(
            TranslationUnit(
                page_number=page_number,
                unit_number=1,
                source_text=normalize_spaces(page_text),
                unit_type="paragraph",
            )
        )

    return units


def build_document_units(pages_text: list[str]) -> list[PageResult]:
    results: list[PageResult] = []

    for page_number, page_text in enumerate(pages_text, start=1):
        results.append(
            PageResult(
                page_number=page_number,
                ocr_text=page_text,
                units=page_to_units(page_text, page_number),
            )
        )

    return results


# ============================================================
# LOCAL NLLB MODEL
# ============================================================

class LocalTranslator:
    def __init__(self) -> None:
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.batch_size = (
            GPU_BATCH_SIZE if self.device == "cuda" else CPU_BATCH_SIZE
        )

        print("=" * 72)
        print("Loading local translation model")
        print(f"Model : {MODEL_NAME}")
        print(f"Device: {self.device}")
        if self.device == "cuda":
            print(f"GPU   : {torch.cuda.get_device_name(0)}")
        print("=" * 72)

        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_NAME,
            src_lang=SOURCE_LANGUAGE,
            tgt_lang=TARGET_LANGUAGE,
            local_files_only=OFFLINE_MODE,
        )

        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            MODEL_NAME,
            torch_dtype=self.dtype,
            low_cpu_mem_usage=True,
            local_files_only=OFFLINE_MODE,
        )
        self.model.to(self.device)
        self.model.eval()

        self.target_language_id = self.tokenizer.convert_tokens_to_ids(
            TARGET_LANGUAGE
        )

    def token_count(self, text: str) -> int:
        return len(
            self.tokenizer(
                text,
                add_special_tokens=False,
                return_attention_mask=False,
            )["input_ids"]
        )

    def split_long_text(self, text: str) -> list[str]:
        text = normalize_spaces(text)

        if self.token_count(text) <= MAX_SOURCE_TOKENS:
            return [text]

        # Split on Marathi/English sentence boundaries first.
        sentences = [
            part.strip()
            for part in re.split(
                r"(?<=[।.!?;:])\s+|\n+",
                text,
            )
            if part.strip()
        ]

        if not sentences:
            sentences = [text]

        chunks: list[str] = []
        current = ""

        for sentence in sentences:
            candidate = sentence if not current else f"{current} {sentence}"

            if self.token_count(candidate) <= MAX_SOURCE_TOKENS:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            # Hard split a single overlong sentence by words.
            words = sentence.split()
            piece = ""

            for word in words:
                candidate_piece = word if not piece else f"{piece} {word}"

                if self.token_count(candidate_piece) <= MAX_SOURCE_TOKENS:
                    piece = candidate_piece
                else:
                    if piece:
                        chunks.append(piece)
                    piece = word

            if piece:
                current = piece

        if current:
            chunks.append(current)

        return chunks

    def _translate_batch(
        self,
        texts: list[str],
        *,
        num_beams: int = NUM_BEAMS,
        max_new_tokens: int = MAX_NEW_TOKENS,
        length_penalty: float = 1.0,
    ) -> list[str]:
        encoded = self.tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=MAX_SOURCE_TOKENS + 16,
        )
        encoded = {
            key: value.to(self.device)
            for key, value in encoded.items()
        }

        with torch.inference_mode():
            generated = self.model.generate(
                **encoded,
                forced_bos_token_id=self.target_language_id,
                max_new_tokens=max_new_tokens,
                num_beams=num_beams,
                length_penalty=length_penalty,
                do_sample=False,
                use_cache=True,
                early_stopping=True,
                no_repeat_ngram_size=3,
            )

        return self.tokenizer.batch_decode(
            generated,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )

    def translate_texts(self, texts: list[str]) -> list[str]:
        """
        Translate a list while preserving list order.

        Texts containing almost no Marathi are treated as literals and copied,
        because they are usually URLs, account heads, reference codes or numbers.
        """
        final_results = [""] * len(texts)
        expanded_chunks: list[str] = []
        chunk_owners: list[int] = []

        for index, source in enumerate(texts):
            source = normalize_spaces(source)

            if devanagari_letter_count(source) < 3:
                final_results[index] = source
                continue

            for chunk in self.split_long_text(source):
                expanded_chunks.append(chunk)
                chunk_owners.append(index)

        translated_chunks: list[str] = []

        for start in range(0, len(expanded_chunks), self.batch_size):
            batch = expanded_chunks[start : start + self.batch_size]

            try:
                translated_chunks.extend(self._translate_batch(batch))
            except torch.cuda.OutOfMemoryError:
                if self.device != "cuda":
                    raise

                logging.warning(
                    "CUDA out of memory. Retrying the same batch one item at a time."
                )
                torch.cuda.empty_cache()

                for item in batch:
                    translated_chunks.extend(self._translate_batch([item]))

        owner_to_parts: dict[int, list[str]] = {}

        for owner, translated in zip(chunk_owners, translated_chunks):
            owner_to_parts.setdefault(owner, []).append(
                normalize_spaces(translated)
            )

        for owner, parts in owner_to_parts.items():
            final_results[owner] = " ".join(part for part in parts if part)

        return final_results

    def recovery_candidates(
        self,
        source: str,
        initial_translation: str,
    ) -> list[str]:
        """
        Generate alternative candidates only for a unit that failed validation.

        NLLB is not instruction-tuned, so recovery changes decoding and,
        when useful, translates smaller sentence pieces rather than adding a
        natural-language prompt.
        """
        candidates: list[str] = []

        if initial_translation.strip():
            candidates.append(normalize_spaces(initial_translation))

        for attempt in range(UNIT_RECOVERY_ATTEMPTS):
            beams = 6 + (attempt * 2)
            penalty = 1.05 + (attempt * 0.05)

            try:
                candidate = self._translate_batch(
                    [source],
                    num_beams=beams,
                    max_new_tokens=max(MAX_NEW_TOKENS, 640),
                    length_penalty=penalty,
                )[0]
                candidate = normalize_spaces(candidate)
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
            except torch.cuda.OutOfMemoryError:
                if self.device == "cuda":
                    torch.cuda.empty_cache()
                logging.warning(
                    "CUDA OOM while recovering one translation unit."
                )

        # A long unit may translate better when split at sentence boundaries.
        parts = [
            part.strip()
            for part in re.split(r"(?<=[।.!?;:])\s+|\n+", source)
            if part.strip()
        ]

        if len(parts) > 1:
            try:
                translated_parts = self.translate_texts(parts)
                joined = normalize_spaces(
                    " ".join(part for part in translated_parts if part.strip())
                )
                if joined and joined not in candidates:
                    candidates.append(joined)
            except Exception:
                logging.exception(
                    "Sentence-level recovery failed for one translation unit."
                )

        return candidates


# ============================================================
# TRANSLATION VALIDATION AND CACHE
# ============================================================

def translation_quality_issue(
    source: str,
    translated: str,
) -> str | None:
    """
    Return a quality warning, or None when the translation is acceptable.

    The previous validator required at least eight output characters for every
    Marathi unit. That incorrectly rejected valid short translations such as
    "Read", "Total", "Date", or "Office".
    """
    source_compact = re.sub(r"\s+", "", source)
    translated_compact = re.sub(r"\s+", "", translated)

    if devanagari_letter_count(source) < 3:
        return None

    if not translated_compact:
        return "The local model returned an empty translation."

    ratio = devanagari_ratio(translated)
    if ratio > MAX_OUTPUT_DEVANAGARI_RATIO:
        return (
            "Too much untranslated Devanagari remains "
            f"({ratio:.1%}; allowed {MAX_OUTPUT_DEVANAGARI_RATIO:.1%})."
        )

    # Length validation is useful only for substantial source units.
    # It must not be applied to one-word headings or table cells.
    if len(source_compact) >= LONG_SOURCE_VALIDATION_MIN_CHARS:
        minimum_length = max(
            4,
            int(
                len(source_compact)
                * MIN_LONG_TRANSLATION_LENGTH_RATIO
            ),
        )
        if len(translated_compact) < minimum_length:
            return (
                "Output is unexpectedly short for a long source unit "
                f"({len(translated_compact)} characters; expected at least "
                f"{minimum_length})."
            )

    return None


def translation_candidate_score(
    source: str,
    translated: str,
) -> tuple[int, float, int]:
    """Lower tuple is better when every candidate still needs review."""
    issue = translation_quality_issue(source, translated)
    empty_penalty = 1 if not translated.strip() else 0
    devanagari_penalty = devanagari_ratio(translated)
    # Prefer a non-empty, cleaner and then more informative candidate.
    return (
        empty_penalty + (1 if issue else 0),
        devanagari_penalty,
        -len(re.sub(r"\s+", "", translated)),
    )


def validate_translation(source: str, translated: str) -> None:
    """Strict wrapper retained for final document validation."""
    issue = translation_quality_issue(source, translated)
    if issue:
        raise RuntimeError(f"Translation quality check failed: {issue}")


def load_translation_cache(
    pdf_path: Path,
    page_results: list[PageResult],
) -> None:
    cache = translation_cache_path(pdf_path)

    if not cache.exists():
        return

    payload = json.loads(cache.read_text(encoding="utf-8"))
    cached = payload.get("translations", {})

    for page in page_results:
        for unit in page.units:
            key = f"{unit.page_number}:{unit.unit_number}"
            cached_item = cached.get(key)

            if not cached_item:
                continue

            if cached_item.get("source_text") != unit.source_text:
                continue

            cached_translation = str(
                cached_item.get("translated_text", "")
            )
            cached_status = str(
                cached_item.get("quality_status", "PASS")
            )
            cached_note = str(
                cached_item.get("quality_note", "")
            )

            issue = translation_quality_issue(
                unit.source_text,
                cached_translation,
            )

            # Reuse clean translations. Review translations are also reusable
            # because they have already exhausted recovery attempts.
            if issue is None or cached_status == "REVIEW":
                unit.translated_text = cached_translation
                unit.quality_status = (
                    cached_status if cached_status else "PASS"
                )
                unit.quality_note = cached_note or (issue or "")

    print(f"  Loaded translation cache: {cache}")


def save_translation_cache(
    pdf_path: Path,
    page_results: list[PageResult],
) -> None:
    translations: dict[str, dict[str, str]] = {}

    for page in page_results:
        for unit in page.units:
            key = f"{unit.page_number}:{unit.unit_number}"
            translations[key] = {
                "source_text": unit.source_text,
                "translated_text": unit.translated_text,
                "unit_type": unit.unit_type,
                "quality_status": unit.quality_status,
                "quality_note": unit.quality_note,
            }

    atomic_write_json(
        translation_cache_path(pdf_path),
        {
            "source_pdf": pdf_path.name,
            "model": MODEL_NAME,
            "source_language": SOURCE_LANGUAGE,
            "target_language": TARGET_LANGUAGE,
            "translations": translations,
        },
    )


def choose_recovered_translation(
    unit: TranslationUnit,
    translator: LocalTranslator,
    initial_translation: str,
) -> tuple[str, str, str]:
    """
    Return (translation, status, note).

    PASS means validation succeeded.
    REVIEW means the best available candidate was kept and written to a review
    CSV so one difficult unit does not destroy the complete PDF.
    """
    candidates = translator.recovery_candidates(
        unit.source_text,
        initial_translation,
    )

    for candidate in candidates:
        issue = translation_quality_issue(
            unit.source_text,
            candidate,
        )
        if issue is None:
            return candidate, "PASS", ""

    non_empty = [candidate for candidate in candidates if candidate.strip()]

    if non_empty:
        best = min(
            non_empty,
            key=lambda candidate: translation_candidate_score(
                unit.source_text,
                candidate,
            ),
        )
        issue = translation_quality_issue(unit.source_text, best)
        note = issue or "Recovery candidate requires manual verification."

        if ALLOW_REVIEW_OUTPUT:
            return best, "REVIEW", note

        raise RuntimeError(
            "Translation recovery failed for "
            f"page {unit.page_number}, unit {unit.unit_number}: {note}"
        )

    if ALLOW_REVIEW_OUTPUT:
        return (
            EMPTY_TRANSLATION_PLACEHOLDER,
            "REVIEW",
            "The local model returned no usable translation after recovery.",
        )

    raise RuntimeError(
        "Translation recovery returned no text for "
        f"page {unit.page_number}, unit {unit.unit_number}."
    )


def translate_document(
    pdf_path: Path,
    page_results: list[PageResult],
    translator: LocalTranslator,
) -> None:
    load_translation_cache(pdf_path, page_results)

    if APPLY_MANUAL_REVIEW_CORRECTIONS:
        correction_count = apply_manual_review_corrections(
            pdf_path,
            page_results,
        )
        if correction_count:
            save_translation_cache(pdf_path, page_results)
            print(
                f"  Applied manual review corrections: {correction_count}"
            )

    pending: list[TranslationUnit] = [
        unit
        for page in page_results
        for unit in page.units
        if not unit.translated_text.strip()
    ]

    total = len(pending)
    if not pending:
        print("  All translation units were loaded from cache.")
        return

    print(f"  Translation units remaining: {total}")

    # Work in groups, but save every group's valid/review units even when one
    # item needs recovery.
    group_size = max(1, translator.batch_size * 4)

    for start in range(0, total, group_size):
        group = pending[start : start + group_size]
        sources = [unit.source_text for unit in group]

        translated_texts = translator.translate_texts(sources)

        for unit, translated in zip(group, translated_texts):
            translated = normalize_spaces(translated)
            issue = translation_quality_issue(
                unit.source_text,
                translated,
            )

            if issue is None:
                unit.translated_text = translated
                unit.quality_status = "PASS"
                unit.quality_note = ""
                continue

            print(
                "  Recovering page "
                f"{unit.page_number}, unit {unit.unit_number}: {issue}"
            )

            recovered, status, note = choose_recovered_translation(
                unit=unit,
                translator=translator,
                initial_translation=translated,
            )
            unit.translated_text = recovered
            unit.quality_status = status
            unit.quality_note = note

        # Atomic cache write after every group provides unit-level resume.
        save_translation_cache(pdf_path, page_results)

        finished = min(start + len(group), total)
        review_count = sum(
            unit.quality_status == "REVIEW"
            for page in page_results
            for unit in page.units
        )
        print(
            f"  Translated units: {finished}/{total} "
            f"| review units: {review_count}"
        )


def validate_complete_document(page_results: list[PageResult]) -> None:
    document_unit_count = 0

    for page in page_results:
        # Blank/sparse pages are allowed. They may be covers or signature pages.
        if not page.units:
            continue

        for unit in page.units:
            document_unit_count += 1

            if not unit.translated_text.strip():
                raise RuntimeError(
                    "Incomplete document: "
                    f"page {unit.page_number}, unit {unit.unit_number} "
                    "has no translation."
                )

            issue = translation_quality_issue(
                unit.source_text,
                unit.translated_text,
            )

            if issue and unit.quality_status != "REVIEW":
                raise RuntimeError(
                    "Incomplete recovery state on "
                    f"page {unit.page_number}, unit {unit.unit_number}: "
                    f"{issue}"
                )

    if document_unit_count == 0:
        raise RuntimeError(
            "No readable text or translation units were found in the PDF."
        )


# ============================================================
# HTML AND PDF OUTPUT
# ============================================================

def document_to_plain_text(page_results: list[PageResult]) -> str:
    pages: list[str] = []

    for page in page_results:
        page_text = "\n\n".join(
            unit.translated_text.strip()
            for unit in page.units
            if unit.translated_text.strip()
        )
        pages.append(
            f"--- Source page {page.page_number} ---\n\n{page_text}"
        )

    return "\n\n".join(pages)


def document_to_html_fragment(page_results: list[PageResult]) -> str:
    html_pages: list[str] = []

    for page_index, page in enumerate(page_results):
        blocks: list[str] = []

        for unit in page.units:
            safe = html.escape(unit.translated_text.strip())

            if unit.unit_type == "table_line":
                blocks.append(f'<div class="table-line">{safe}</div>')
            elif unit.unit_type == "literal":
                blocks.append(f'<div class="literal">{safe}</div>')
            else:
                blocks.append(f"<p>{safe}</p>")

        page_break = (
            '<div class="source-page-break"></div>'
            if page_index > 0
            else ""
        )

        html_pages.append(
            page_break
            + f'<section class="source-page" data-page="{page.page_number}">'
            + "\n".join(blocks)
            + "</section>"
        )

    return "\n".join(html_pages)


def build_complete_html(
    fragment: str,
    source_filename: str,
) -> str:
    safe_source = html.escape(source_filename)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_source} - Local English Translation</title>
<style>
  @page {{
    size: A4;
    margin: 16mm 15mm 18mm 15mm;
  }}

  * {{
    box-sizing: border-box;
  }}

  body {{
    margin: 0;
    font-family: Arial, "Noto Sans", sans-serif;
    font-size: 10.4pt;
    line-height: 1.48;
    color: #111;
  }}

  .source-page {{
    width: 100%;
  }}

  .source-page-break {{
    break-before: page;
    page-break-before: always;
  }}

  p {{
    margin: 0 0 7pt 0;
    text-align: justify;
    orphans: 3;
    widows: 3;
  }}

  .table-line {{
    border-bottom: 0.35pt solid #bbb;
    padding: 3pt 2pt;
    white-space: pre-wrap;
    page-break-inside: avoid;
  }}

  .literal {{
    font-family: "Courier New", monospace;
    white-space: pre-wrap;
    margin-bottom: 5pt;
  }}

  .translation-note {{
    margin-top: 15pt;
    padding-top: 6pt;
    border-top: 0.5pt solid #999;
    color: #555;
    font-size: 8pt;
  }}
</style>
</head>
<body>
{fragment}
<div class="translation-note">
Local machine translation generated from source file: {safe_source}. 
This output is for research use and requires human verification.
</div>
</body>
</html>
"""


def write_outputs(
    pdf_path: Path,
    page_results: list[PageResult],
) -> tuple[Path, Path, Path]:
    text_path = output_text_path(pdf_path)
    html_path = output_html_path(pdf_path)
    pdf_path_out = output_pdf_path(pdf_path)

    plain_text = document_to_plain_text(page_results)
    atomic_write_text(text_path, plain_text)

    fragment = document_to_html_fragment(page_results)
    complete_html = build_complete_html(fragment, pdf_path.name)
    atomic_write_text(html_path, complete_html)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(complete_html, wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=str(pdf_path_out),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font-size:8px;color:#666;'
                'text-align:center;">'
                '<span class="pageNumber"></span> / '
                '<span class="totalPages"></span>'
                "</div>"
            ),
            margin={
                "top": "16mm",
                "right": "15mm",
                "bottom": "18mm",
                "left": "15mm",
            },
        )
        browser.close()

    return text_path, html_path, pdf_path_out


def read_existing_manual_corrections(
    pdf_path: Path,
) -> dict[tuple[int, int], str]:
    report_path = review_report_path(pdf_path)

    if not report_path.exists():
        return {}

    corrections: dict[tuple[int, int], str] = {}

    try:
        with report_path.open(
            "r",
            newline="",
            encoding="utf-8-sig",
        ) as handle:
            for row in csv.DictReader(handle):
                corrected = str(
                    row.get("corrected_english", "")
                ).strip()
                if not corrected:
                    continue

                key = (
                    int(row["page"]),
                    int(row["unit"]),
                )
                corrections[key] = corrected
    except Exception:
        logging.exception(
            "Could not read manual corrections from %s",
            report_path,
        )

    return corrections


def apply_manual_review_corrections(
    pdf_path: Path,
    page_results: list[PageResult],
) -> int:
    corrections = read_existing_manual_corrections(pdf_path)

    if not corrections:
        return 0

    applied = 0

    for page in page_results:
        for unit in page.units:
            key = (unit.page_number, unit.unit_number)
            corrected = corrections.get(key)

            if not corrected:
                continue

            unit.translated_text = corrected
            unit.quality_status = "PASS"
            unit.quality_note = "Manually corrected from review CSV."
            applied += 1

    return applied


def write_review_report(
    pdf_path: Path,
    page_results: list[PageResult],
) -> tuple[Path | None, int]:
    review_units = [
        unit
        for page in page_results
        for unit in page.units
        if unit.quality_status == "REVIEW"
    ]

    if not review_units:
        return None, 0

    report_path = review_report_path(pdf_path)
    existing_corrections = read_existing_manual_corrections(pdf_path)

    with report_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source_pdf",
                "page",
                "unit",
                "unit_type",
                "quality_note",
                "source_marathi_ocr",
                "english_candidate",
                "corrected_english",
            ]
        )

        for unit in review_units:
            writer.writerow(
                [
                    pdf_path.name,
                    unit.page_number,
                    unit.unit_number,
                    unit.unit_type,
                    unit.quality_note,
                    unit.source_text,
                    unit.translated_text,
                    existing_corrections.get(
                        (unit.page_number, unit.unit_number),
                        "",
                    ),
                ]
            )

    return report_path, len(review_units)


# ============================================================
# FAILURE/RESUME HANDLING
# ============================================================

def read_failure_attempts(marker: Path) -> int:
    if not marker.exists():
        return 0

    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return int(payload.get("attempt_count", 1))
    except Exception:
        return 1


def write_failed_marker(
    pdf_path: Path,
    pages: int,
    error: Exception,
) -> Path:
    marker = failed_marker_path(pdf_path)
    attempt_count = read_failure_attempts(marker) + 1

    atomic_write_json(
        marker,
        {
            "source_pdf": pdf_path.name,
            "pages": pages,
            "attempt_count": attempt_count,
            "failed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        },
    )
    return marker


def should_skip_pdf(pdf_path: Path) -> tuple[bool, str]:
    output = output_pdf_path(pdf_path)
    marker = failed_marker_path(pdf_path)

    if output.exists() and not FORCE_RETRANSLATE:
        return True, "English PDF already exists"

    if RUN_MODE not in {"failed_only", "resume", "new_only"}:
        raise ValueError(
            "RUN_MODE must be 'failed_only', 'resume', or 'new_only'."
        )

    if RUN_MODE == "failed_only" and not marker.exists():
        return True, "not a previously failed PDF"

    if RUN_MODE == "new_only" and marker.exists():
        return True, "previous failure marker exists"

    if marker.exists():
        attempts = read_failure_attempts(marker)

        if attempts >= MAX_PDF_FAILURE_ATTEMPTS:
            return (
                True,
                f"failure retry limit reached ({attempts} attempts)",
            )

    return False, ""


# ============================================================
# PROCESSING
# ============================================================

def process_one_pdf(
    pdf_path: Path,
    translator: LocalTranslator,
) -> bool:
    started = time.perf_counter()
    pages = get_page_count(pdf_path)
    device = translator.device

    print("=" * 72)
    print(f"Processing: {pdf_path.name}")
    print(f"Pages     : {pages}")

    try:
        pages_text = extract_or_load_ocr(pdf_path)
        page_results = build_document_units(pages_text)

        translate_document(
            pdf_path=pdf_path,
            page_results=page_results,
            translator=translator,
        )
        validate_complete_document(page_results)

        text_path, html_path, pdf_output = write_outputs(
            pdf_path,
            page_results,
        )
        review_path, review_count = write_review_report(
            pdf_path,
            page_results,
        )

        marker = failed_marker_path(pdf_path)
        if (
            REMOVE_FAILED_MARKER_AFTER_SUCCESS
            and marker.exists()
        ):
            marker.unlink()

        elapsed = time.perf_counter() - started
        status = "SUCCESS_REVIEW" if review_count else "SUCCESS"
        message = (
            f"{review_count} unit(s) require manual review: {review_path}"
            if review_count
            else ""
        )

        append_csv_log(
            filename=pdf_path.name,
            status=status,
            pages=pages,
            seconds=elapsed,
            device=device,
            message=message,
        )
        logging.info(
            "%s | %s | pages=%s | device=%s | review_units=%s | %.2fs",
            pdf_path.name,
            status,
            pages,
            device,
            review_count,
            elapsed,
        )

        print(f"Saved text: {text_path}")
        print(f"Saved HTML: {html_path}")
        print(f"Saved PDF : {pdf_output}")
        if review_path:
            print(f"Review CSV: {review_path}")
            print(f"Review units: {review_count}")
        print(f"Status    : {status}")
        print(f"Time      : {elapsed:.1f} seconds")
        return True

    except Exception as error:
        elapsed = time.perf_counter() - started
        marker = write_failed_marker(pdf_path, pages, error)

        append_csv_log(
            filename=pdf_path.name,
            status="FAILED_SKIPPED",
            pages=pages,
            seconds=elapsed,
            device=device,
            message=str(error),
        )
        logging.exception("%s | FAILED_SKIPPED", pdf_path.name)

        print(f"FAILED AND SKIPPED: {pdf_path.name}")
        print(f"Reason            : {error}")
        print(f"Failure marker    : {marker}")
        print("The program will continue with the next PDF.")
        return False


def main() -> None:
    verify_tesseract()

    pdfs = sorted(INPUT_FOLDER.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(
            f"No PDF files were found in:\n{INPUT_FOLDER}"
        )

    pending: list[Path] = []
    skipped_existing = 0
    skipped_by_mode = 0
    skipped_retry_limit = 0

    for pdf_path in pdfs:
        skip, reason = should_skip_pdf(pdf_path)

        if skip:
            if reason == "English PDF already exists":
                skipped_existing += 1
            elif "retry limit" in reason:
                skipped_retry_limit += 1
            else:
                skipped_by_mode += 1
            continue

        pending.append(pdf_path)

    if MAX_NEW_FILES > 0:
        pending = pending[:MAX_NEW_FILES]

    print("=" * 72)
    print("Local Marathi-to-English PDF Translator")
    print(f"Input folder             : {INPUT_FOLDER}")
    print(f"Output folder            : {OUTPUT_FOLDER}")
    print(f"Run mode                 : {RUN_MODE}")
    print(f"Total source PDFs        : {len(pdfs)}")
    print(f"Existing outputs skipped : {skipped_existing}")
    print(f"Skipped by run mode      : {skipped_by_mode}")
    print(f"Retry-limit skipped      : {skipped_retry_limit}")
    print(f"PDFs selected this run   : {len(pending)}")
    print("=" * 72)

    if not pending:
        print("Nothing to process.")
        return

    # Load the model only when at least one PDF needs processing.
    translator = LocalTranslator()

    successful = 0
    failed = 0

    for position, pdf_path in enumerate(pending, start=1):
        print(f"\nDocument {position}/{len(pending)}")

        if process_one_pdf(pdf_path, translator):
            successful += 1
        else:
            failed += 1

    print("=" * 72)
    print("Run finished")
    print(f"Successful     : {successful}")
    print(f"Failed/skipped : {failed}")
    print(f"CSV log        : {CSV_LOG}")
    print("=" * 72)


if __name__ == "__main__":
    main()
