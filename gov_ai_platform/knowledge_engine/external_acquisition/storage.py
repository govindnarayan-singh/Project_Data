from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class PdfProbe:
    page_count: int
    probe_text: str


def safe_filename(identifier: str, suffix: str = ".pdf") -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", identifier).strip("._-")[:120] or "document"
    return stem + suffix.casefold()


def validate_pdf(body: bytes, *, probe_pages: int = 3) -> PdfProbe:
    if not body.startswith(b"%PDF"):
        raise ValueError("Downloaded payload does not have a PDF signature")
    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted external PDFs are not accepted")
        page_count = len(reader.pages)
        if page_count < 1:
            raise ValueError("External PDF has no pages")
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:probe_pages])
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"External PDF parsing failed: {error}") from error
    return PdfProbe(page_count=page_count, probe_text=text)


def store_verified_file(root: Path, *, identifier: str, body: bytes) -> tuple[Path, str, bool]:
    validate_pdf(body)
    resolved_root = root.resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    target = (resolved_root / safe_filename(f"{identifier}-{digest[:12]}")).resolve()
    target.relative_to(resolved_root)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("Existing acquisition file has a conflicting hash")
        return target, digest, False
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(target)
    return target, digest, True


__all__ = ["PdfProbe", "safe_filename", "store_verified_file", "validate_pdf"]
