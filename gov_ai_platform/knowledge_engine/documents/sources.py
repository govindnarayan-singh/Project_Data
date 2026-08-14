from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knowledge_engine.settings import settings


LOCAL_ORIGINAL = "LOCAL_ORIGINAL"
EXTERNAL_OFFICIAL = "EXTERNAL_OFFICIAL"
APPROVED_ORIGINS = frozenset({LOCAL_ORIGINAL, EXTERNAL_OFFICIAL})
LOCAL_GR_ROOT = "LOCAL_GR"
EXTERNAL_OFFICIAL_ROOT = "EXTERNAL_OFFICIAL"


class UnsafeDocumentPath(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentSource:
    origin: str
    source_path: str

    def resolve(self) -> Path:
        return resolve_document_path(self.origin, self.source_path)


def source_root(origin: str) -> Path:
    if origin == LOCAL_ORIGINAL:
        return settings.gr_root
    if origin == EXTERNAL_OFFICIAL:
        return settings.external_document_root
    raise UnsafeDocumentPath(f"Unsupported document source origin: {origin}")


def source_root_identity(origin: str) -> str:
    if origin == LOCAL_ORIGINAL:
        return LOCAL_GR_ROOT
    if origin == EXTERNAL_OFFICIAL:
        return EXTERNAL_OFFICIAL_ROOT
    raise UnsafeDocumentPath(f"Unsupported document source origin: {origin}")


def resolve_document_path(origin: str, stored_path: str) -> Path:
    """Resolve a database path strictly within the origin's approved root."""
    root = source_root(origin).resolve(strict=True)
    supplied = Path(stored_path)
    if supplied.is_absolute():
        raise UnsafeDocumentPath("Stored document paths must be relative")
    candidate = (root / supplied).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeDocumentPath("Document path is outside its approved source root") from error
    if candidate.suffix.lower() != ".pdf":
        raise UnsafeDocumentPath("Document is not a PDF")
    return candidate
