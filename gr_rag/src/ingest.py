from __future__ import annotations

import argparse
import logging
from pathlib import Path

from tqdm import tqdm

from .chunker import TokenChunker
from .config import PROJECT_ROOT, settings
from .db import Database
from .embedder import Embedder
from .metadata import extract_metadata
from .pdf_extract import extract_pdf
from .text_utils import normalize_marathi_text, sha256_file


def setup_logging() -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "ingest.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def discover_pdfs(root: Path, department: str | None = None) -> list[Path]:
    if department:
        dept_root = root / department
        if not dept_root.exists():
            raise FileNotFoundError(f"Department folder not found: {dept_root}")
        return sorted(dept_root.rglob("*.pdf"))
    return sorted(root.rglob("*.pdf"))


def department_from_path(pdf_path: Path, root: Path) -> str:
    relative = pdf_path.relative_to(root)
    if len(relative.parts) < 2:
        return "Unknown"
    return relative.parts[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Marathi GR PDFs into PostgreSQL + pgvector")
    parser.add_argument("--root", type=Path, default=settings.gr_root)
    parser.add_argument("--department", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Reprocess even if file SHA already exists")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback")
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Diagnostic mode: OCR every page even when a digital text layer exists",
    )
    args = parser.parse_args()

    if args.no_ocr and args.force_ocr:
        parser.error("--no-ocr and --force-ocr cannot be used together")

    setup_logging()
    root = args.root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"GR root does not exist: {root}")

    db = Database()
    db.ensure_schema()

    pdfs = discover_pdfs(root, args.department)
    if args.limit is not None:
        pdfs = pdfs[: args.limit]

    logging.info("Found %d PDFs under %s", len(pdfs), root)
    if not pdfs:
        return

    # Load once and reuse across all documents.
    chunker = TokenChunker()
    embedder = Embedder()

    success = 0
    skipped = 0
    failed = 0

    for pdf_path in tqdm(pdfs, desc="Ingesting GRs"):
        try:
            digest = sha256_file(pdf_path)
            if not args.force and db.exists_sha(digest):
                skipped += 1
                continue

            department = department_from_path(pdf_path, root)
            extracted = extract_pdf(
                pdf_path,
                allow_ocr=not args.no_ocr,
                force_ocr=args.force_ocr,
            )
            normalized_text = normalize_marathi_text(extracted.normalized_text)

            if len(normalized_text) < 100:
                raise RuntimeError("Very little text was extracted from the document")

            metadata = extract_metadata(normalized_text, department, pdf_path.name)
            chunks = chunker.chunk_pages(extracted.pages)
            if not chunks:
                raise RuntimeError("No chunks were created")

            embeddings = embedder.encode([c.text for c in chunks])

            db.replace_document(
                sha256=digest,
                file_path=str(pdf_path.relative_to(root)),
                file_name=pdf_path.name,
                department=department,
                page_count=len(extracted.pages),
                digital_pages=extracted.digital_pages,
                ocr_pages=extracted.ocr_pages,
                extraction_method=extracted.extraction_method,
                raw_text=extracted.raw_text,
                normalized_text=normalized_text,
                extracted_metadata=metadata,
                pages=extracted.pages,
                chunks=chunks,
                embeddings=embeddings,
            )

            success += 1
            logging.info(
                "OK | %s | dept=%s | method=%s | pages=%d | chunks=%d | gr=%s",
                pdf_path.name,
                department,
                extracted.extraction_method,
                len(extracted.pages),
                len(chunks),
                metadata.get("gr_number"),
            )
        except Exception as exc:
            failed += 1
            logging.exception("FAILED | %s | %s", pdf_path, exc)

    logging.info("DONE | success=%d skipped=%d failed=%d", success, skipped, failed)


if __name__ == "__main__":
    main()
