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
   - failed PDFs get a marker and are skipped on later runs unless
     RETRY_FAILED is changed to True;
   - completed OCR and page translations are cached.

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
    "GRs/Electronics, Information Technology and Artificial Intelligence Department"
)

# Keep this identical to the Gemini output directory.
# The script will automatically skip the 50 PDFs already translated there.
OUTPUT_FOLDER = Path(
    "translated/Electronics, Information Technology and Artificial Intelligence Department"
)

HTML_FOLDER = OUTPUT_FOLDER / "html_local"
TEXT_FOLDER = OUTPUT_FOLDER / "text_local"

CACHE_ROOT = Path("cache_local")
OCR_CACHE_FOLDER = CACHE_ROOT / "ocr"
TRANSLATION_CACHE_FOLDER = CACHE_ROOT / "translations"
FAILED_FOLDER = CACHE_ROOT / "failed"

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
    LOG_FOLDER,
):
    folder.mkdir(parents=True, exist_ok=True)

# 0 means process every remaining PDF.
# For the first test, set this to 1.
MAX_NEW_FILES = 51

# Existing English PDFs are always skipped unless this is True.
FORCE_RETRANSLATE = False

# Failed marker files are skipped on future runs unless this is True.
RETRY_FAILED = False

# Delete a failed marker automatically after a successful retry.
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

# Validation.
MAX_OUTPUT_DEVANAGARI_RATIO = 0.04
MIN_TRANSLATION_LENGTH_RATIO = 0.10

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


def ocr_page(image: Image.Image) -> str:
    config = (
        f"--oem 1 --psm {OCR_PSM} "
        "-c preserve_interword_spaces=1"
    )

    text = pytesseract.image_to_string(
        image,
        lang=OCR_LANGUAGE,
        config=config,
    )
    return clean_ocr_text(text)


def validate_ocr_page(text: str, page_number: int) -> None:
    compact = re.sub(r"\s+", "", text)

    if len(compact) < MIN_OCR_CHARACTERS_PER_PAGE:
        raise RuntimeError(
            f"OCR quality check failed on page {page_number}: "
            f"only {len(compact)} non-space characters were detected."
        )

    # Some pages may contain mostly codes/numbers, so this threshold is low.
    ratio = devanagari_ratio(text)
    if ratio < MIN_OCR_DEVANAGARI_RATIO and devanagari_letter_count(text) < 10:
        raise RuntimeError(
            f"OCR quality check failed on page {page_number}: "
            f"very little Marathi was detected ({ratio:.1%})."
        )


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
            text = ocr_page(image)
            validate_ocr_page(text, page_number)
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

    def _translate_batch(self, texts: list[str]) -> list[str]:
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
                max_new_tokens=MAX_NEW_TOKENS,
                num_beams=NUM_BEAMS,
                do_sample=False,
                use_cache=True,
                early_stopping=True,
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


# ============================================================
# TRANSLATION VALIDATION AND CACHE
# ============================================================

def validate_translation(source: str, translated: str) -> None:
    source_compact = re.sub(r"\s+", "", source)
    translated_compact = re.sub(r"\s+", "", translated)

    if devanagari_letter_count(source) < 3:
        return

    if not translated_compact:
        raise RuntimeError("The local model returned an empty translation.")

    ratio = devanagari_ratio(translated)
    if ratio > MAX_OUTPUT_DEVANAGARI_RATIO:
        raise RuntimeError(
            "Translation quality check failed: "
            f"{ratio:.1%} Devanagari remains."
        )

    minimum_length = max(
        8,
        int(len(source_compact) * MIN_TRANSLATION_LENGTH_RATIO),
    )
    if len(translated_compact) < minimum_length:
        raise RuntimeError(
            "Translation quality check failed: output is unexpectedly short "
            f"({len(translated_compact)} characters; expected at least "
            f"{minimum_length})."
        )


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

            unit.translated_text = str(
                cached_item.get("translated_text", "")
            )

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


def translate_document(
    pdf_path: Path,
    page_results: list[PageResult],
    translator: LocalTranslator,
) -> None:
    load_translation_cache(pdf_path, page_results)

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

    # Work in groups, save after each group.
    group_size = max(1, translator.batch_size * 4)

    for start in range(0, total, group_size):
        group = pending[start : start + group_size]
        sources = [unit.source_text for unit in group]

        translated_texts = translator.translate_texts(sources)

        for unit, translated in zip(group, translated_texts):
            validate_translation(unit.source_text, translated)
            unit.translated_text = translated

        save_translation_cache(pdf_path, page_results)

        finished = min(start + len(group), total)
        print(f"  Translated units: {finished}/{total}")


def validate_complete_document(page_results: list[PageResult]) -> None:
    for page in page_results:
        if not page.units:
            raise RuntimeError(
                f"No translation units were created for page {page.page_number}."
            )

        for unit in page.units:
            if not unit.translated_text.strip():
                raise RuntimeError(
                    "Incomplete document: "
                    f"page {unit.page_number}, unit {unit.unit_number} "
                    "has no translation."
                )
            validate_translation(unit.source_text, unit.translated_text)


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


# ============================================================
# FAILURE/RESUME HANDLING
# ============================================================

def write_failed_marker(
    pdf_path: Path,
    pages: int,
    error: Exception,
) -> Path:
    marker = failed_marker_path(pdf_path)

    atomic_write_json(
        marker,
        {
            "source_pdf": pdf_path.name,
            "pages": pages,
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

    if marker.exists() and not RETRY_FAILED:
        return True, "previous failure marker exists"

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

        marker = failed_marker_path(pdf_path)
        if (
            REMOVE_FAILED_MARKER_AFTER_SUCCESS
            and marker.exists()
        ):
            marker.unlink()

        elapsed = time.perf_counter() - started

        append_csv_log(
            filename=pdf_path.name,
            status="SUCCESS",
            pages=pages,
            seconds=elapsed,
            device=device,
        )
        logging.info(
            "%s | SUCCESS | pages=%s | device=%s | %.2fs",
            pdf_path.name,
            pages,
            device,
            elapsed,
        )

        print(f"Saved text: {text_path}")
        print(f"Saved HTML: {html_path}")
        print(f"Saved PDF : {pdf_output}")
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
    skipped_failed = 0

    for pdf_path in pdfs:
        skip, reason = should_skip_pdf(pdf_path)

        if skip:
            if "failure marker" in reason:
                skipped_failed += 1
            else:
                skipped_existing += 1
            continue

        pending.append(pdf_path)

    if MAX_NEW_FILES > 0:
        pending = pending[:MAX_NEW_FILES]

    print("=" * 72)
    print("Local Marathi-to-English PDF Translator")
    print(f"Input folder             : {INPUT_FOLDER}")
    print(f"Output folder            : {OUTPUT_FOLDER}")
    print(f"Total source PDFs        : {len(pdfs)}")
    print(f"Existing outputs skipped : {skipped_existing}")
    print(f"Failed markers skipped   : {skipped_failed}")
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
