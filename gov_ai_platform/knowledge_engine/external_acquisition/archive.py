from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from knowledge_engine.settings import settings

from .storage import validate_pdf


ARCHIVE_ROOT_IDENTITY = "EXTERNAL_OFFICIAL"
ARCHIVE_STATUS = "EXTERNAL_OFFICIAL_ARCHIVED"

DOCUMENT_KIND_DIRECTORIES = {
    "ACT": "Acts",
    "RULE": "Rules",
    "GOVERNMENT_RESOLUTION": "Government_Resolutions",
    "CIRCULAR": "Circulars",
    "GOVERNMENT_CIRCULAR": "Circulars",
    "NOTIFICATION": "Notifications",
    "GOVERNMENT_ORDER": "Government_Orders",
    "COURT_ORDER": "Court_Documents",
    "COURT_DOCUMENT": "Court_Documents",
}

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ArchiveSafetyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    archive_root_identity: str
    archive_relative_path: str
    safe_local_filename: str
    sha256: str
    file_size: int
    page_count: int
    archive_status: str
    archive_timestamp: str
    created: bool
    byte_equal: bool


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _incoming_root() -> Path:
    return getattr(settings, "external_incoming_root", settings.external_acquisition_root)


def ensure_archive_root(root: Path | None = None) -> Path:
    root = root or settings.external_document_root
    resolved = root.resolve(strict=False)
    gr_root = settings.gr_root.resolve(strict=False)
    incoming = _incoming_root().resolve(strict=False)
    if resolved == gr_root or _is_within(resolved, gr_root) or _is_within(gr_root, resolved):
        raise ArchiveSafetyError("EXTERNAL_DOCUMENT_ROOT must not overlap GR_ROOT")
    if resolved == incoming or _is_within(resolved, incoming) or _is_within(incoming, resolved):
        raise ArchiveSafetyError("EXTERNAL_DOCUMENT_ROOT must not overlap EXTERNAL_INCOMING_ROOT")
    resolved.mkdir(parents=True, exist_ok=True)
    probe = resolved / f".archive-write-probe-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"archive-root-check")
    finally:
        if probe.exists():
            probe.unlink()
    return resolved


def archive_directory(document_kind: str) -> str:
    canonical = (document_kind or "OTHER").strip().upper()
    return DOCUMENT_KIND_DIRECTORIES.get(canonical, "Other")


def _ascii_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^0-9A-Za-z]+", "_", ascii_value).strip(" ._-")
    slug = re.sub(r"_+", "_", slug)
    return slug


def safe_archive_filename(document_kind: str, identity: str, sha256: str) -> str:
    digest = sha256.strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ArchiveSafetyError("Archive filename requires a valid SHA-256")
    kind = _ascii_slug(document_kind).upper() or "OTHER"
    identity_slug = _ascii_slug(identity) or "DOCUMENT"
    stem = f"{kind}_{identity_slug}_{digest[:12]}"[:176].rstrip(" .")
    if stem.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        stem = f"DOCUMENT_{stem}"
    return f"{stem}.pdf"


def validate_archive_relative_path(relative_path: str) -> PurePosixPath:
    if not relative_path or "\\" in relative_path:
        raise ArchiveSafetyError("Archive path must be a non-empty POSIX relative path")
    supplied = PurePosixPath(relative_path)
    if supplied.is_absolute() or any(part in {"", ".", ".."} for part in supplied.parts):
        raise ArchiveSafetyError("Archive path traversal or absolute paths are forbidden")
    if supplied.suffix.casefold() != ".pdf":
        raise ArchiveSafetyError("Archive path must identify a PDF")
    return supplied


def resolve_archive_path(
    relative_path: str, root: Path | None = None, *, must_exist: bool = False
) -> Path:
    root = root or settings.external_document_root
    resolved_root = root.resolve(strict=must_exist)
    relative = validate_archive_relative_path(relative_path)
    candidate = (resolved_root / Path(*relative.parts)).resolve(strict=False)
    if not _is_within(candidate, resolved_root):
        raise ArchiveSafetyError("Archive path is outside EXTERNAL_DOCUMENT_ROOT")
    if must_exist and not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def promote_verified_file(
    source_path: Path,
    *,
    document_kind: str,
    identity: str,
    expected_sha256: str,
    root: Path | None = None,
) -> ArchiveResult:
    source = source_path.resolve(strict=True)
    incoming = _incoming_root().resolve(strict=True)
    if not _is_within(source, incoming):
        raise ArchiveSafetyError("Only controlled Incoming files may be promoted")
    if source.suffix.casefold() != ".pdf":
        raise ArchiveSafetyError("Only PDF files may be archived")
    body = source.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != expected_sha256.casefold():
        raise ArchiveSafetyError("Incoming SHA-256 does not match verified provenance")
    source_size = len(body)
    probe = validate_pdf(body)
    archive_root = ensure_archive_root(root)
    filename = safe_archive_filename(document_kind, identity, digest)
    relative_path = PurePosixPath(archive_directory(document_kind), filename).as_posix()
    destination = resolve_archive_path(relative_path, archive_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    if destination.exists():
        existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_digest != digest:
            raise ArchiveSafetyError("Archive path collision has different immutable bytes")
    else:
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise ArchiveSafetyError("Archive copy SHA-256 verification failed")
            os.replace(temporary, destination)
            created = True
        finally:
            if temporary.exists():
                temporary.unlink()
    archived = destination.read_bytes()
    archived_digest = hashlib.sha256(archived).hexdigest()
    if len(archived) != source_size or archived_digest != digest or archived != body:
        raise ArchiveSafetyError("Archived source is not byte-identical to Incoming")
    validate_pdf(archived)
    try:
        destination.chmod(0o444)
    except OSError:
        pass
    return ArchiveResult(
        archive_root_identity=ARCHIVE_ROOT_IDENTITY,
        archive_relative_path=relative_path,
        safe_local_filename=filename,
        sha256=digest,
        file_size=source_size,
        page_count=probe.page_count,
        archive_status=ARCHIVE_STATUS,
        archive_timestamp=datetime.now(timezone.utc).isoformat(),
        created=created,
        byte_equal=True,
    )


__all__ = [
    "ARCHIVE_ROOT_IDENTITY",
    "ARCHIVE_STATUS",
    "ArchiveResult",
    "ArchiveSafetyError",
    "archive_directory",
    "ensure_archive_root",
    "promote_verified_file",
    "resolve_archive_path",
    "safe_archive_filename",
    "validate_archive_relative_path",
]
