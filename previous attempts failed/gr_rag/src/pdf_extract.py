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
    MARATHI_SIGNAL_WORDS,
    digital_text_classification,
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


def _mean_word_confidence(tsv_text: str) -> float:
    weighted_total = 0.0
    character_count = 0
    for line in tsv_text.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12:
            continue
        word = fields[11].strip()
        if not word:
            continue
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        if confidence < 0:
            continue
        weight = max(len(word), 1)
        weighted_total += confidence * weight
        character_count += weight
    return weighted_total / character_count if character_count else -1.0


def _select_ocr_candidate(candidates: list[tuple[str, float]]) -> str:
    """Prefer Tesseract confidence, using text quality only as a tie-breaker."""
    return max(
        candidates,
        key=lambda candidate: (candidate[1], extracted_text_quality(candidate[0])),
    )[0]


def _ocr_page(
    pdfium_doc: pdfium.PdfDocument,
    page_index: int,
    try_rotations: bool = False,
) -> str:
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

    if not try_rotations:
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

    candidates: list[tuple[str, float]] = []
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        for angle in (0, 90, 270):
            candidate_image = image if angle == 0 else image.rotate(angle, expand=True)
            image_path = tmp_dir / f"page_{angle}.png"
            output_base = tmp_dir / f"ocr_{angle}"
            candidate_image.save(image_path, format="PNG")
            cmd = [
                settings.tesseract_cmd,
                str(image_path),
                str(output_base),
                "-l",
                settings.ocr_lang,
                "--psm",
                "6",
                "--dpi",
                str(settings.ocr_dpi),
                "txt",
                "tsv",
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
                    f"Tesseract failed on page {page_index + 1} at {angle} degrees: "
                    f"{completed.stderr.strip()}"
                )
            candidate_text = normalize_marathi_text(
                output_base.with_suffix(".txt").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
            confidence = _mean_word_confidence(
                output_base.with_suffix(".tsv").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
            candidates.append((candidate_text, confidence))
    return _select_ocr_candidate(candidates)


def _should_use_ocr(digital_text: str, ocr_text: str, force_ocr: bool) -> bool:
    if force_ocr:
        return True
    if len(ocr_text) < 40:
        return False

    digital_quality = extracted_text_quality(digital_text)
    ocr_quality = extracted_text_quality(ocr_text)
    ocr_repairs_mapping = malformed_devanagari_count(
        ocr_text
    ) < malformed_devanagari_count(digital_text)
    digital_signal_count = sum(word in digital_text for word in MARATHI_SIGNAL_WORDS)
    ocr_signal_count = sum(word in ocr_text for word in MARATHI_SIGNAL_WORDS)
    uncertain_signal_repair = (
        digital_text_classification(digital_text) == "UNCERTAIN"
        and ocr_quality >= digital_quality
        and ocr_signal_count > digital_signal_count
    )
    return (
        ocr_quality >= digital_quality + 0.05
        or ocr_repairs_mapping
        or uncertain_signal_repair
    )


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
                ocr_text = _ocr_page(
                    pdfium_doc,
                    idx,
                    try_rotations=(
                        digital_text_classification(digital_text)
                        == "SUSPICIOUS_GARBLED_LATIN"
                    ),
                )
                # Forced OCR is diagnostic and therefore unconditional.  In automatic
                # mode prefer OCR when it is linguistically better, while retaining a
                # usable digital layer if OCR genuinely fails.
                use_ocr = _should_use_ocr(digital_text, ocr_text, force_ocr)
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
