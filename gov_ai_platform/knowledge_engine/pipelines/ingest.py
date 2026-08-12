from __future__ import annotations

import argparse
import logging
from pathlib import Path

from knowledge_engine.chunking.chunker import TokenChunker
from knowledge_engine.embeddings.bge_m3 import get_embedder
from knowledge_engine.extraction.pdf import extract_pdf
from knowledge_engine.extraction.text_quality import normalize_marathi_text, sha256_file
from knowledge_engine.metadata.extractor import extract_metadata
from knowledge_engine.metadata.validation import validate_metadata
from knowledge_engine.settings import PROJECT_ROOT, settings
from knowledge_engine.storage.postgres import PostgresStorage


def discover_pdfs(root: Path, department: str | None = None) -> list[Path]:
    search_root = root / department if department else root
    if not search_root.exists():
        raise FileNotFoundError(f"GR folder not found: {search_root}")
    return sorted(search_root.rglob("*.pdf"))


def department_from_path(pdf_path: Path, root: Path) -> str:
    relative = pdf_path.relative_to(root)
    return relative.parts[0] if len(relative.parts) >= 2 else "Unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Marathi GR PDFs into PostgreSQL + pgvector")
    parser.add_argument("--root", type=Path, default=settings.gr_root)
    parser.add_argument("--department")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--force-ocr", action="store_true")
    args = parser.parse_args()
    if args.no_ocr and args.force_ocr:
        parser.error("--no-ocr and --force-ocr cannot be used together")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    database = PostgresStorage()
    database.ensure_schema()
    pdfs = discover_pdfs(args.root.resolve(), args.department)
    if args.limit is not None:
        pdfs = pdfs[:args.limit]
    chunker = TokenChunker()
    embedder = get_embedder()
    for pdf_path in pdfs:
        digest = sha256_file(pdf_path)
        if not args.force and database.exists_sha(digest):
            continue
        extracted = extract_pdf(pdf_path, allow_ocr=not args.no_ocr, force_ocr=args.force_ocr)
        normalized = normalize_marathi_text(extracted.normalized_text)
        metadata = extract_metadata(normalized, department_from_path(pdf_path, args.root.resolve()), pdf_path.name)
        metadata_validation = validate_metadata(metadata)
        if metadata_validation.has_warnings:
            logging.warning(
                "Metadata validation warning for %s: %s",
                pdf_path,
                metadata_validation.as_dict(),
            )
        chunks = chunker.chunk_pages(extracted.pages)
        vectors = embedder.encode([chunk.text for chunk in chunks], show_progress_bar=True)
        database.replace_document(
            sha256=digest, file_path=str(pdf_path.relative_to(args.root.resolve())), file_name=pdf_path.name,
            department=metadata["department"], page_count=len(extracted.pages), digital_pages=extracted.digital_pages,
            ocr_pages=extracted.ocr_pages, extraction_method=extracted.extraction_method, raw_text=extracted.raw_text,
            normalized_text=normalized, extracted_metadata=metadata, pages=extracted.pages, chunks=chunks, embeddings=vectors,
        )


if __name__ == "__main__":
    main()
