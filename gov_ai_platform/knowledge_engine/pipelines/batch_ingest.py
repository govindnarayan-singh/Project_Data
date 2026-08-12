from __future__ import annotations

import csv
import hashlib
import json
import shutil
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from knowledge_engine.chunking.chunker import TokenChunker
from knowledge_engine.embeddings.bge_m3 import EmbeddingProvider, get_embedder
from knowledge_engine.extraction.pdf import _tesseract_available, extract_pdf
from knowledge_engine.extraction.text_quality import normalize_marathi_text, sha256_file
from knowledge_engine.metadata.extractor import extract_metadata
from knowledge_engine.metadata.validation import MetadataValidation, validate_metadata
from knowledge_engine.settings import PROJECT_ROOT, settings
from knowledge_engine.storage.postgres import PostgresStorage


REQUIRED_MANIFEST_COLUMNS = frozenset({"file_path", "department"})
OPTIONAL_MANIFEST_COLUMNS = frozenset({"document_label", "notes"})
FAILURE_STATUSES = frozenset(
    {
        "FAILED_EXTRACTION",
        "FAILED_METADATA",
        "FAILED_CHUNKING",
        "FAILED_EMBEDDING",
        "FAILED_DATABASE",
    }
)
COMPLETED_STATUSES = frozenset(
    {"SUCCESS", "SUCCESS_WITH_WARNING", "SKIPPED_UNCHANGED"}
)


class ManifestValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Manifest validation failed:\n- " + "\n- ".join(errors))


class Operation(str, Enum):
    NEW = "NEW"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"
    REPROCESSED = "REPROCESSED"


class Status(str, Enum):
    SUCCESS = "SUCCESS"
    SUCCESS_WITH_WARNING = "SUCCESS_WITH_WARNING"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"
    DRY_RUN = "DRY_RUN"
    FAILED_EXTRACTION = "FAILED_EXTRACTION"
    FAILED_METADATA = "FAILED_METADATA"
    FAILED_CHUNKING = "FAILED_CHUNKING"
    FAILED_EMBEDDING = "FAILED_EMBEDDING"
    FAILED_DATABASE = "FAILED_DATABASE"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    row_number: int
    file_path: str
    department: str
    document_label: str
    notes: str
    absolute_path: Path

    @property
    def file_name(self) -> str:
        return self.absolute_path.name

    @property
    def database_path(self) -> str:
        return str(Path(self.file_path))


@dataclass(slots=True)
class BatchResult:
    run_id: str
    file_path: str
    file_name: str
    department: str
    sha256: str = ""
    operation: str = ""
    status: str = ""
    pages_total: int = 0
    pages_digital: int = 0
    pages_ocr: int = 0
    gr_number: str = ""
    gr_number_validation: str = ""
    gr_number_validation_reason: str = ""
    gr_date: str = ""
    gr_date_validation: str = ""
    gr_date_validation_reason: str = ""
    subject: str = ""
    subject_validation: str = ""
    subject_validation_reason: str = ""
    chunks_created: int = 0
    min_chunk_tokens: int = 0
    max_chunk_tokens: int = 0
    avg_chunk_tokens: float = 0.0
    embedding_count: int = 0
    embedding_dimension: int = 0
    elapsed_seconds: float = 0.0
    error_stage: str = ""
    error_message: str = ""
    document_label: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [field.name for field in fields(cls)]

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "BatchResult":
        integer_fields = {
            "pages_total", "pages_digital", "pages_ocr", "chunks_created",
            "min_chunk_tokens", "max_chunk_tokens", "embedding_count",
            "embedding_dimension",
        }
        float_fields = {"avg_chunk_tokens", "elapsed_seconds"}
        values: dict[str, Any] = dict(row)
        for name in integer_fields:
            values[name] = int(values.get(name) or 0)
        for name in float_fields:
            values[name] = float(values.get(name) or 0.0)
        return cls(**{name: values.get(name, "") for name in cls.fieldnames()})


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_manifest(path: Path, gr_root: Path = settings.gr_root) -> list[ManifestEntry]:
    path = path.resolve(strict=True)
    root = gr_root.resolve(strict=True)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headings = set(reader.fieldnames or [])
        missing = REQUIRED_MANIFEST_COLUMNS - headings
        if missing:
            raise ManifestValidationError(
                [f"Missing required columns: {', '.join(sorted(missing))}"]
            )
        rows = list(reader)

    errors: list[str] = []
    entries: list[ManifestEntry] = []
    seen_paths: dict[str, int] = {}
    for row_number, row in enumerate(rows, start=2):
        raw_path = (row.get("file_path") or "").strip()
        department = (row.get("department") or "").strip()
        if not raw_path:
            errors.append(f"Row {row_number}: file_path is empty")
            continue
        if not department:
            errors.append(f"Row {row_number}: department is empty")
            continue

        supplied = Path(raw_path)
        candidate = (
            supplied.resolve(strict=False)
            if supplied.is_absolute()
            else (root / supplied).resolve(strict=False)
        )
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            errors.append(f"Row {row_number}: path is outside GR_ROOT: {raw_path}")
            continue
        if candidate.suffix.lower() != ".pdf":
            errors.append(f"Row {row_number}: path is not a PDF: {raw_path}")
            continue
        if not candidate.is_file():
            errors.append(f"Row {row_number}: PDF does not exist: {raw_path}")
            continue

        normalized_path = relative.as_posix()
        duplicate_key = normalized_path.casefold()
        if duplicate_key in seen_paths:
            errors.append(
                f"Row {row_number}: duplicate file_path also appears at row "
                f"{seen_paths[duplicate_key]}: {normalized_path}"
            )
            continue
        seen_paths[duplicate_key] = row_number
        entries.append(
            ManifestEntry(
                row_number=row_number,
                file_path=normalized_path,
                department=department,
                document_label=(row.get("document_label") or "").strip(),
                notes=(row.get("notes") or "").strip(),
                absolute_path=candidate,
            )
        )

    if not rows:
        errors.append("Manifest contains no document rows")
    if errors:
        raise ManifestValidationError(errors)
    return entries


class BatchIngestor:
    """Application service for one manifest, with per-document isolation."""

    def __init__(
        self,
        *,
        storage: PostgresStorage | None = None,
        chunker: TokenChunker | None = None,
        embedder: EmbeddingProvider | None = None,
        embedder_factory: Callable[[], EmbeddingProvider] = get_embedder,
        extract_pdf_fn: Callable = extract_pdf,
        extract_metadata_fn: Callable = extract_metadata,
        validate_metadata_fn: Callable = validate_metadata,
        sha256_fn: Callable[[Path], str] = sha256_file,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.storage = storage or PostgresStorage()
        self.chunker = chunker
        self._embedder = embedder
        self.embedder_factory = embedder_factory
        self.extract_pdf_fn = extract_pdf_fn
        self.extract_metadata_fn = extract_metadata_fn
        self.validate_metadata_fn = validate_metadata_fn
        self.sha256_fn = sha256_fn
        self.clock = clock

    @property
    def embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = self.embedder_factory()
        return self._embedder

    @property
    def token_chunker(self) -> TokenChunker:
        if self.chunker is None:
            self.chunker = TokenChunker()
        return self.chunker

    def _operation(
        self, entry: ManifestEntry, digest: str, force: bool
    ) -> tuple[Operation, dict[str, Any] | None]:
        identity = self.storage.find_document_identity(entry.database_path, digest)
        if identity is None:
            return Operation.NEW, None
        if force or identity["sha256"] != digest:
            return Operation.REPROCESSED, identity
        return Operation.SKIPPED_UNCHANGED, identity

    def preflight(
        self, entries: Iterable[ManifestEntry], *, force: bool = False
    ) -> None:
        """Fail once for infrastructure required by the planned operations."""
        self.storage.counts()
        requires_processing = force
        if not requires_processing:
            for entry in entries:
                digest = self.sha256_fn(entry.absolute_path)
                operation, _identity = self._operation(entry, digest, force=False)
                if operation is not Operation.SKIPPED_UNCHANGED:
                    requires_processing = True
                    break
        if requires_processing and not _tesseract_available():
            raise RuntimeError(
                "Tesseract is unavailable. Set TESSERACT_CMD before a live batch run."
            )
        if requires_processing:
            # Model initialization is an infrastructure-wide dependency. Load
            # once here so a cache/model failure stops the run rather than
            # producing the same per-document error repeatedly.
            self.embedder

    @staticmethod
    def _apply_validation(
        result: BatchResult, validation: MetadataValidation
    ) -> None:
        for field_name in ("gr_number", "gr_date", "subject"):
            field_validation = getattr(validation, field_name)
            setattr(result, f"{field_name}_validation", field_validation.status.value)
            setattr(result, f"{field_name}_validation_reason", field_validation.reason)

    def process_entry(
        self,
        entry: ManifestEntry,
        *,
        run_id: str,
        force: bool = False,
        dry_run: bool = False,
    ) -> BatchResult:
        started = self.clock()
        result = BatchResult(
            run_id=run_id,
            file_path=entry.file_path,
            file_name=entry.file_name,
            department=entry.department,
            document_label=entry.document_label,
            notes=entry.notes,
        )

        try:
            result.sha256 = self.sha256_fn(entry.absolute_path)
            operation, _identity = self._operation(entry, result.sha256, force)
            result.operation = operation.value
        except Exception as error:
            return self._failure(result, Status.FAILED_DATABASE, "identity", error, started)

        if dry_run:
            result.status = (
                Status.SKIPPED_UNCHANGED.value
                if operation is Operation.SKIPPED_UNCHANGED
                else Status.DRY_RUN.value
            )
            result.elapsed_seconds = round(self.clock() - started, 6)
            return result
        if operation is Operation.SKIPPED_UNCHANGED:
            result.status = Status.SKIPPED_UNCHANGED.value
            result.elapsed_seconds = round(self.clock() - started, 6)
            return result

        try:
            extracted = self.extract_pdf_fn(entry.absolute_path)
            if not extracted.pages:
                raise ValueError("No pages were extracted")
            result.pages_total = len(extracted.pages)
            result.pages_digital = extracted.digital_pages
            result.pages_ocr = extracted.ocr_pages
            normalized = normalize_marathi_text(extracted.normalized_text)
            if not normalized:
                raise ValueError("Extracted document text is empty")
        except Exception as error:
            return self._failure(result, Status.FAILED_EXTRACTION, "extraction", error, started)

        try:
            metadata = self.extract_metadata_fn(
                normalized, entry.department, entry.file_name
            )
            metadata_validation = self.validate_metadata_fn(metadata)
            result.gr_number = str(metadata.get("gr_number") or "")
            result.gr_date = (
                metadata["gr_date"].isoformat() if metadata.get("gr_date") else ""
            )
            result.subject = str(metadata.get("subject") or "")
            self._apply_validation(result, metadata_validation)
        except Exception as error:
            return self._failure(result, Status.FAILED_METADATA, "metadata", error, started)

        try:
            chunks = self.token_chunker.chunk_pages(extracted.pages)
            if not chunks:
                raise ValueError("No chunks were created")
            violating = [
                chunk for chunk in chunks
                if chunk.token_count > settings.chunk_max_tokens
            ]
            if violating:
                details = ", ".join(
                    f"index={chunk.chunk_index}:tokens={chunk.token_count}"
                    for chunk in violating[:10]
                )
                raise ValueError(
                    f"Chunks exceed CHUNK_MAX_TOKENS={settings.chunk_max_tokens}: {details}"
                )
            token_counts = [chunk.token_count for chunk in chunks]
            result.chunks_created = len(chunks)
            result.min_chunk_tokens = min(token_counts)
            result.max_chunk_tokens = max(token_counts)
            result.avg_chunk_tokens = round(statistics.fmean(token_counts), 3)
        except Exception as error:
            return self._failure(result, Status.FAILED_CHUNKING, "chunking", error, started)

        try:
            embeddings = self.embedder.encode(
                [chunk.text for chunk in chunks], show_progress_bar=False
            )
            result.embedding_count = len(embeddings)
            if result.embedding_count != len(chunks):
                raise ValueError(
                    f"Embedding count {result.embedding_count} does not match "
                    f"chunk count {len(chunks)}"
                )
            dimensions = {len(embedding) for embedding in embeddings}
            if dimensions != {settings.embed_dim}:
                raise ValueError(
                    f"Embedding dimensions {sorted(dimensions)} do not match "
                    f"EMBED_DIM={settings.embed_dim}"
                )
            result.embedding_dimension = settings.embed_dim
        except Exception as error:
            return self._failure(result, Status.FAILED_EMBEDDING, "embedding", error, started)

        try:
            self.storage.replace_document(
                sha256=result.sha256,
                file_path=entry.database_path,
                file_name=entry.file_name,
                department=entry.department,
                page_count=result.pages_total,
                digital_pages=result.pages_digital,
                ocr_pages=result.pages_ocr,
                extraction_method=extracted.extraction_method,
                raw_text=extracted.raw_text,
                normalized_text=normalized,
                extracted_metadata=metadata,
                pages=extracted.pages,
                chunks=chunks,
                embeddings=embeddings,
            )
        except Exception as error:
            return self._failure(result, Status.FAILED_DATABASE, "database", error, started)

        result.status = (
            Status.SUCCESS_WITH_WARNING.value
            if metadata_validation.has_warnings
            else Status.SUCCESS.value
        )
        result.elapsed_seconds = round(self.clock() - started, 6)
        return result

    def _failure(
        self,
        result: BatchResult,
        status: Status,
        stage: str,
        error: Exception,
        started: float,
    ) -> BatchResult:
        result.status = status.value
        result.error_stage = stage
        result.error_message = f"{type(error).__name__}: {error}"[:2000]
        result.elapsed_seconds = round(self.clock() - started, 6)
        return result


def _atomic_csv(path: Path, rows: Iterable[BatchResult]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BatchResult.fieldnames())
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    temporary.replace(path)


def _summary(
    run_id: str,
    manifest_path: Path,
    manifest_hash: str,
    results: list[BatchResult],
    dry_run: bool,
) -> dict[str, Any]:
    statuses = Counter(result.status for result in results)
    operations = Counter(result.operation for result in results)
    metadata_counts: dict[str, dict[str, int]] = {}
    for field_name in ("gr_number", "gr_date", "subject"):
        metadata_counts[field_name] = dict(
            Counter(
                getattr(result, f"{field_name}_validation")
                for result in results
                if getattr(result, f"{field_name}_validation")
            )
        )
    successful = [
        result for result in results
        if result.status in {Status.SUCCESS.value, Status.SUCCESS_WITH_WARNING.value}
    ]
    return {
        "run_id": run_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "dry_run": dry_run,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "document_count": len(results),
        "status_counts": dict(statuses),
        "operation_counts": dict(operations),
        "metadata_validation": metadata_counts,
        "chunks_created": sum(result.chunks_created for result in successful),
        "min_chunk_tokens": min(
            (result.min_chunk_tokens for result in successful), default=0
        ),
        "max_chunk_tokens": max(
            (result.max_chunk_tokens for result in successful), default=0
        ),
        "embedding_count": sum(result.embedding_count for result in successful),
        "embedding_dimensions": sorted(
            {result.embedding_dimension for result in successful if result.embedding_dimension}
        ),
        "failure_count": sum(status in FAILURE_STATUSES for status in statuses.elements()),
    }


def _write_run_outputs(
    run_dir: Path,
    run_id: str,
    manifest_path: Path,
    manifest_hash: str,
    results: list[BatchResult],
    dry_run: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_csv(run_dir / "results.csv", results)
    failures = [result for result in results if result.status in FAILURE_STATUSES]
    errors_path = run_dir / "errors.csv"
    if failures:
        _atomic_csv(errors_path, failures)
    elif errors_path.exists():
        errors_path.unlink()
    summary = _summary(run_id, manifest_path, manifest_hash, results, dry_run)
    temporary = run_dir / "summary.json.tmp"
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(run_dir / "summary.json")
    return summary


def _load_results(path: Path) -> list[BatchResult]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [BatchResult.from_csv_row(row) for row in csv.DictReader(handle)]


def _find_resume_run(output_root: Path, manifest_hash: str) -> Path:
    candidates: list[Path] = []
    for summary_path in output_root.glob("*/summary.json"):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("manifest_sha256") == manifest_hash and not summary.get("dry_run"):
            candidates.append(summary_path.parent)
    if not candidates:
        raise ValueError("No resumable non-dry-run exists for this manifest")
    return max(candidates, key=lambda candidate: candidate.name)


def execute_manifest(
    manifest_path: Path,
    *,
    gr_root: Path = settings.gr_root,
    ingestor: BatchIngestor | None = None,
    output_root: Path | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    resume: bool = False,
    force: bool = False,
    on_result: Callable[[BatchResult], None] | None = None,
) -> tuple[Path, list[BatchResult], dict[str, Any]]:
    if resume and force:
        raise ValueError("--resume and --force cannot be combined")
    manifest_path = manifest_path.resolve(strict=True)
    entries = parse_manifest(manifest_path, gr_root=gr_root)
    digest = manifest_sha256(manifest_path)
    output_root = (output_root or PROJECT_ROOT / "outputs" / "bulk_ingestion").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if run_id and not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            "run_id may contain only letters, numbers, hyphens, and underscores"
        )

    if resume:
        run_dir = (
            output_root / run_id if run_id else _find_resume_run(output_root, digest)
        )
        if not run_dir.is_dir():
            raise ValueError(f"Resume run does not exist: {run_dir}")
        stored_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        if stored_summary.get("manifest_sha256") != digest:
            raise ValueError("Resume manifest hash does not match the frozen run manifest")
        run_id = run_dir.name
    else:
        run_id = run_id or (
            datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
            + f"-{manifest_path.stem}-{digest[:8]}"
        )
        run_dir = output_root / run_id
        if run_dir.exists():
            raise ValueError(f"Run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        shutil.copyfile(manifest_path, run_dir / "manifest_snapshot.csv")

    ingestor = ingestor or BatchIngestor()
    if not dry_run and hasattr(ingestor, "preflight"):
        ingestor.preflight(entries, force=force)
    previous = _load_results(run_dir / "results.csv") if resume else []
    results_by_path = {result.file_path.casefold(): result for result in previous}
    ordered_results: list[BatchResult] = []

    for entry in entries:
        key = entry.file_path.casefold()
        prior = results_by_path.get(key)
        if (
            resume
            and prior is not None
            and prior.status in COMPLETED_STATUSES
            and prior.sha256 == sha256_file(entry.absolute_path)
        ):
            result = prior
        else:
            result = ingestor.process_entry(
                entry, run_id=run_id, force=force, dry_run=dry_run
            )
        results_by_path[key] = result
        ordered_results.append(result)
        _write_run_outputs(
            run_dir, run_id, manifest_path, digest, ordered_results, dry_run
        )
        if on_result:
            on_result(result)

    summary = _write_run_outputs(
        run_dir, run_id, manifest_path, digest, ordered_results, dry_run
    )
    return run_dir, ordered_results, summary
