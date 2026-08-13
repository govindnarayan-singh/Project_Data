from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from pypdf import PdfReader

from knowledge_engine.metadata.extractor import date_from_filename

from .models import LocalPdf


class UnsafeCorpusPath(ValueError):
    pass


def resolve_inside_root(root: Path, path: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise UnsafeCorpusPath(f"PDF is outside GR_ROOT: {path}") from error
    if resolved.suffix.casefold() != ".pdf":
        raise UnsafeCorpusPath(f"Not a PDF: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect(root: Path, path: Path, ingested_paths: set[str], ingested_sha: set[str]) -> LocalPdf:
    try:
        safe = resolve_inside_root(root, path)
        relative = safe.relative_to(root.resolve()).as_posix()
        digest = _sha256(safe)
        try:
            page_count = len(PdfReader(str(safe), strict=False).pages)
            error = ""
        except Exception as exc:
            page_count = None
            error = f"PAGE_COUNT:{type(exc).__name__}"
        return LocalPdf(
            relative_path=relative, department=relative.split("/", 1)[0], file_name=safe.name,
            file_size=safe.stat().st_size, page_count=page_count,
            filename_date=date_from_filename(safe.name), sha256=digest,
            already_ingested=relative.casefold() in ingested_paths or digest in ingested_sha,
            inventory_error=error,
        )
    except Exception as exc:
        return LocalPdf(str(path), "", path.name, 0, None, None, "", False, f"PATH:{type(exc).__name__}:{exc}")


def build_inventory(
    root: Path, *, ingested_paths: set[str], ingested_sha: set[str], workers: int = 8,
) -> list[LocalPdf]:
    resolved_root = root.resolve(strict=True)
    paths = sorted(
        (path for path in resolved_root.rglob("*") if path.is_file() and path.suffix.casefold() == ".pdf"),
        key=lambda value: value.relative_to(resolved_root).as_posix().casefold(),
    )
    normalized_paths = {value.replace("\\", "/").casefold() for value in ingested_paths}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        items = list(pool.map(lambda path: _inspect(resolved_root, path, normalized_paths, ingested_sha), paths))
    return items
