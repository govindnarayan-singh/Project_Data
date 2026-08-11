from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageOps
from pypdf import PdfReader

from .config import settings
from .text_utils import (
    extracted_text_quality,
    malformed_devanagari_count,
    normalize_marathi_text,
    should_ocr,
)


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    method: str  # digital | ocr
    quality_score: float


@dataclass
class ExtractedDocument:
    pages: list[ExtractedPage]

    @property
    def raw_text(self) -> str:
        return "\n\n".join(
            f"--- PAGE {p.page_number} ---\n{p.text}" for p in self.pages
        )

    @property
    def normalized_text(self) -> str:
        return normalize_marathi_text(self.raw_text)

    @property
    def digital_pages(self) -> int:
        return sum(p.method == "digital" for p in self.pages)

    @property
    def ocr_pages(self) -> int:
        return sum(p.method == "ocr" for p in self.pages)

    @property
    def extraction_method(self) -> str:
        if self.ocr_pages == 0:
            return "digital"
        if self.digital_pages == 0:
            return "ocr"
        return "hybrid"


def _tesseract_available() -> bool:
    cmd = settings.tesseract_cmd
    if Path(cmd).is_file():
        return True
    return shutil.which(cmd) is not None


def _ocr_page(pdfium_doc: pdfium.PdfDocument, page_index: int) -> str:
    if not _tesseract_available():
        raise RuntimeError(
            "Tesseract was required for an image/poor-text page but was not found. "
            "Install Tesseract with Marathi language data or set TESSERACT_CMD in .env."
        )

    page = pdfium_doc[page_index]
    scale = settings.ocr_dpi / 72.0
    bitmap = page.render(scale=scale)
    image = bitmap.to_pil()

    # Simple preprocessing. Do not binarize aggressively because old scanned GRs vary a lot.
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        image.save(tmp_path, format="PNG")

        cmd = [
            settings.tesseract_cmd,
            str(tmp_path),
            "stdout",
            "-l",
            settings.ocr_lang,
            "--psm",
            "6",
            "--dpi",
            str(settings.ocr_dpi),
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Tesseract failed on page {page_index + 1}: {completed.stderr.strip()}"
            )
        return normalize_marathi_text(completed.stdout)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def extract_pdf(
    path: Path,
    allow_ocr: bool = True,
    force_ocr: bool = False,
) -> ExtractedDocument:
    reader = PdfReader(str(path), strict=False)
    pdfium_doc = pdfium.PdfDocument(str(path)) if allow_ocr else None

    pages: list[ExtractedPage] = []
    try:
        for idx, page in enumerate(reader.pages):
            try:
                digital_text = page.extract_text() or ""
            except Exception:
                digital_text = ""

            digital_text = normalize_marathi_text(digital_text)

            if allow_ocr and (force_ocr or should_ocr(digital_text)):
                assert pdfium_doc is not None
                ocr_text = _ocr_page(pdfium_doc, idx)
                digital_quality = extracted_text_quality(digital_text)
                ocr_quality = extracted_text_quality(ocr_text)
                ocr_repairs_mapping = malformed_devanagari_count(ocr_text) < malformed_devanagari_count(
                    digital_text
                )
                # Forced OCR is diagnostic and therefore unconditional.  In automatic
                # mode prefer OCR when it is linguistically better, while retaining a
                # usable digital layer if OCR genuinely fails.
                use_ocr = force_ocr or (
                    len(ocr_text) >= 40
                    and (ocr_quality >= digital_quality + 0.05 or ocr_repairs_mapping)
                )
                if use_ocr:
                    text = ocr_text
                    method = "ocr"
                else:
                    text = digital_text
                    method = "digital"
            else:
                text = digital_text
                method = "digital"

            pages.append(
                ExtractedPage(
                    page_number=idx + 1,
                    text=text,
                    method=method,
                    quality_score=extracted_text_quality(text),
                )
            )
    finally:
        if pdfium_doc is not None:
            pdfium_doc.close()

    return ExtractedDocument(pages=pages)
