from __future__ import annotations

from pathlib import Path


class UnsafeDocumentPath(ValueError):
    pass


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
