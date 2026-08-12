from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from knowledge_engine.extraction.text_quality import (
    MARATHI_SIGNAL_WORDS,
    digital_text_classification,
    extracted_text_quality,
    malformed_devanagari_count,
    normalize_marathi_text,
    should_ocr,
)
from knowledge_engine.settings import settings


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    method: str
    quality_score: float


@dataclass
class ExtractedDocument:
    pages: list[ExtractedPage]

    @property
    def raw_text(self) -> str:
        return "\n\n".join(f"--- PAGE {page.page_number} ---\n{page.text}" for page in self.pages)

    @property
    def normalized_text(self) -> str:
        return normalize_marathi_text(self.raw_text)

    @property
    def digital_pages(self) -> int:
        return sum(page.method == "digital" for page in self.pages)

    @property
    def ocr_pages(self) -> int:
        return sum(page.method == "ocr" for page in self.pages)

    @property
    def extraction_method(self) -> str:
        if self.ocr_pages == 0:
            return "digital"
        if self.digital_pages == 0:
            return "ocr"
        return "hybrid"


def _tesseract_available() -> bool:
    return Path(settings.tesseract_cmd).is_file() or shutil.which(settings.tesseract_cmd) is not None


def _mean_word_confidence(tsv_text: str) -> float:
    weighted_total = 0.0
    character_count = 0
    for line in tsv_text.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12 or not fields[11].strip():
            continue
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        if confidence < 0:
            continue
        weight = max(len(fields[11].strip()), 1)
        weighted_total += confidence * weight
        character_count += weight
    return weighted_total / character_count if character_count else -1.0


def _select_ocr_candidate(candidates: list[tuple[str, float]]) -> str:
    return max(candidates, key=lambda candidate: (candidate[1], extracted_text_quality(candidate[0])))[0]


def _ocr_page(pdfium_doc, page_index: int, try_rotations: bool = False) -> str:
    from PIL import ImageOps

    if not _tesseract_available():
        raise RuntimeError(
            "Tesseract was required for an image/poor-text page but was not found. "
            "Install Tesseract with Marathi language data or set TESSERACT_CMD in .env."
        )
    page = pdfium_doc[page_index]
    image = ImageOps.autocontrast(ImageOps.grayscale(page.render(scale=settings.ocr_dpi / 72.0).to_pil()))
    if not try_rotations:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            image.save(temporary_path, format="PNG")
            command = [
                settings.tesseract_cmd, str(temporary_path), "stdout", "-l", settings.ocr_lang,
                "--psm", "6", "--dpi", str(settings.ocr_dpi),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"Tesseract failed on page {page_index + 1}: {completed.stderr.strip()}")
            return normalize_marathi_text(completed.stdout)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
    angles = (0, 90, 270)
    candidates: list[tuple[str, float]] = []
    with tempfile.TemporaryDirectory() as directory:
        temp_dir = Path(directory)
        for angle in angles:
            candidate = image if angle == 0 else image.rotate(angle, expand=True)
            image_path = temp_dir / f"page_{angle}.png"
            output_base = temp_dir / f"ocr_{angle}"
            candidate.save(image_path, format="PNG")
            command = [
                settings.tesseract_cmd, str(image_path), str(output_base), "-l", settings.ocr_lang,
                "--psm", "6", "--dpi", str(settings.ocr_dpi), "txt", "tsv",
            ]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"Tesseract failed on page {page_index + 1} at {angle} degrees: {completed.stderr.strip()}")
            candidate_text = normalize_marathi_text(output_base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace"))
            confidence = _mean_word_confidence(output_base.with_suffix(".tsv").read_text(encoding="utf-8", errors="replace"))
            candidates.append((candidate_text, confidence))
    return _select_ocr_candidate(candidates)


def _should_use_ocr(digital_text: str, ocr_text: str, force_ocr: bool) -> bool:
    if force_ocr:
        return True
    if len(ocr_text) < 40:
        return False
    digital_quality = extracted_text_quality(digital_text)
    ocr_quality = extracted_text_quality(ocr_text)
    ocr_repairs_mapping = malformed_devanagari_count(ocr_text) < malformed_devanagari_count(digital_text)
    digital_signal_count = sum(word in digital_text for word in MARATHI_SIGNAL_WORDS)
    ocr_signal_count = sum(word in ocr_text for word in MARATHI_SIGNAL_WORDS)
    uncertain_signal_repair = (
        digital_text_classification(digital_text) == "UNCERTAIN"
        and ocr_quality >= digital_quality
        and ocr_signal_count > digital_signal_count
    )
    return ocr_quality >= digital_quality + 0.05 or ocr_repairs_mapping or uncertain_signal_repair


def extract_pdf(path: Path, allow_ocr: bool = True, force_ocr: bool = False) -> ExtractedDocument:
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    pdfium_doc = pdfium.PdfDocument(str(path)) if allow_ocr else None
    pages: list[ExtractedPage] = []
    try:
        for index, page in enumerate(reader.pages):
            try:
                digital_text = normalize_marathi_text(page.extract_text() or "")
            except Exception:
                digital_text = ""
            text, method = digital_text, "digital"
            if allow_ocr and (force_ocr or should_ocr(digital_text)):
                assert pdfium_doc is not None
                ocr_text = _ocr_page(
                    pdfium_doc,
                    index,
                    try_rotations=digital_text_classification(digital_text) == "SUSPICIOUS_GARBLED_LATIN",
                )
                if _should_use_ocr(digital_text, ocr_text, force_ocr):
                    text, method = ocr_text, "ocr"
            pages.append(ExtractedPage(index + 1, text, method, extracted_text_quality(text)))
    finally:
        if pdfium_doc is not None:
            pdfium_doc.close()
    return ExtractedDocument(pages)
