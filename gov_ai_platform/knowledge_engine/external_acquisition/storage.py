from __future__ import annotations

import hashlib
import re
from pathlib import Path


def safe_filename(identifier: str, suffix: str = ".pdf") -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", identifier).strip("._-")[:120] or "document"
    return stem + suffix.casefold()


def store_verified_file(root: Path, *, identifier: str, body: bytes) -> tuple[Path, str, bool]:
    if not body.startswith(b"%PDF"):
        raise ValueError("Acquisition storage accepts only verified PDF signatures")
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


__all__ = ["safe_filename", "store_verified_file"]
