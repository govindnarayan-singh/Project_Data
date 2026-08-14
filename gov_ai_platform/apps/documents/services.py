from __future__ import annotations

from pathlib import Path

from knowledge_engine.documents.sources import UnsafeDocumentPath, resolve_document_path


def resolve_pdf_path(gr_root: Path, stored_path: str) -> Path:
    """Resolve a database path and prove that it remains within GR_ROOT."""
    root = gr_root.resolve(strict=True)
    supplied = Path(stored_path)
    candidate = supplied.resolve(strict=False) if supplied.is_absolute() else (root / supplied).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeDocumentPath("Document path is outside GR_ROOT") from error
    if candidate.suffix.lower() != ".pdf":
        raise UnsafeDocumentPath("Document is not a PDF")
    return candidate


def resolve_pdf_source(
    source_origin: str, stored_path: str, *, local_root: Path | None = None
) -> Path:
    """Resolve a PDF from its explicit approved source class."""
    if source_origin == "LOCAL_ORIGINAL" and local_root is not None:
        return resolve_pdf_path(local_root, stored_path)
    return resolve_document_path(source_origin, stored_path)
