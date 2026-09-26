from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from pypdf import PdfReader

from knowledge_engine.extraction.text_quality import normalize_marathi_text
from knowledge_engine.metadata.extractor import extract_metadata

from .models import MalwareStatus


class DocumentValidationError(ValueError):
    def __init__(self, code: str, message: str, *, findings: list[str] | None = None):
        self.code = code
        self.safe_message = message
        self.findings = findings or []
        super().__init__(message)


@dataclass(frozen=True)
class PdfValidationResult:
    page_count: int
    preview_text: str
    extraction_status: str
    detected_metadata: dict
    findings: list[str]
    risk_flags: list[str]
    malware_status: str


def _scan(path: Path) -> str:
    scanner = settings.DOCUMENT_MALWARE_SCANNER
    executable = settings.DOCUMENT_MALWARE_SCANNER_EXECUTABLE
    if not scanner or not executable:
        return MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED
    command: list[str]
    if scanner == "CLAMAV":
        command = [executable, "--no-summary", str(path)]
    elif scanner == "WINDOWS_DEFENDER":
        command = [executable, "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"]
    else:
        return MalwareStatus.SCAN_ERROR
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.DOCUMENT_VALIDATION_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return MalwareStatus.SCAN_ERROR
    if completed.returncode == 0:
        return MalwareStatus.CLEAN
    if scanner == "CLAMAV" and completed.returncode == 1:
        return MalwareStatus.MALICIOUS
    if scanner == "WINDOWS_DEFENDER" and completed.returncode == 2:
        return MalwareStatus.MALICIOUS
    return MalwareStatus.SCAN_ERROR


def _active_content(reader: PdfReader) -> list[str]:
    flags: set[str] = set()
    try:
        root = reader.trailer["/Root"].get_object()
        names = root.get("/Names")
        if names:
            names = names.get_object()
            if names.get("/EmbeddedFiles"):
                flags.add("EMBEDDED_FILES")
            if names.get("/JavaScript"):
                flags.add("JAVASCRIPT")
        if root.get("/OpenAction") or root.get("/AA"):
            flags.add("DOCUMENT_ACTION")
        for page in reader.pages:
            page_object = page.get_object()
            if page_object.get("/AA"):
                flags.add("PAGE_ACTION")
            for annotation in (page_object.get("/Annots") or []):
                item = annotation.get_object()
                if item.get("/A") or item.get("/AA"):
                    flags.add("ANNOTATION_ACTION")
    except Exception as exc:
        raise DocumentValidationError("MALFORMED_PDF", "The PDF structure could not be inspected safely.") from exc
    return sorted(flags)


def validate_pdf(path: Path, *, original_filename: str, declared_mime: str, department: str) -> PdfValidationResult:
    started = time.monotonic()
    if Path(original_filename).suffix.casefold() != ".pdf":
        raise DocumentValidationError("UNSUPPORTED_EXTENSION", "Only PDF files are accepted.")
    if declared_mime.casefold().split(";", 1)[0].strip() != "application/pdf":
        raise DocumentValidationError("MIME_MISMATCH", "The upload must declare application/pdf.")
    size = path.stat().st_size
    if size == 0:
        raise DocumentValidationError("EMPTY_FILE", "The uploaded file is empty.")
    if size > settings.DOCUMENT_MAX_UPLOAD_BYTES:
        raise DocumentValidationError("FILE_TOO_LARGE", "The PDF exceeds the configured upload limit.")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise DocumentValidationError("INVALID_PDF_SIGNATURE", "The file does not have a valid PDF signature.")
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise DocumentValidationError("MALFORMED_PDF", "The PDF cannot be parsed.") from exc
    if reader.is_encrypted:
        raise DocumentValidationError("ENCRYPTED_PDF", "Encrypted or password-protected PDFs are not accepted.")
    page_count = len(reader.pages)
    if page_count == 0:
        raise DocumentValidationError("ZERO_PAGE_PDF", "The PDF contains no pages.")
    if page_count > settings.DOCUMENT_MAX_PAGES:
        raise DocumentValidationError("PAGE_LIMIT_EXCEEDED", "The PDF exceeds the configured page limit.")
    risk_flags = _active_content(reader)
    if risk_flags:
        raise DocumentValidationError(
            "ACTIVE_CONTENT_DETECTED",
            "PDF attachments, JavaScript, or automatic actions are not accepted.",
            findings=risk_flags,
        )
    preview_parts: list[str] = []
    extraction_errors = 0
    for page in reader.pages[: settings.DOCUMENT_PREVIEW_PAGES]:
        try:
            preview_parts.append(page.extract_text() or "")
        except Exception:
            extraction_errors += 1
        if time.monotonic() - started > settings.DOCUMENT_VALIDATION_TIMEOUT_SECONDS:
            raise DocumentValidationError("VALIDATION_TIMEOUT", "PDF validation exceeded the configured time limit.")
    preview = normalize_marathi_text("\n".join(preview_parts))[: settings.DOCUMENT_PREVIEW_CHARACTERS]
    try:
        metadata = extract_metadata(preview, department, original_filename) if preview else {}
        metadata = json.loads(json.dumps(metadata, default=str))
    except Exception:
        metadata = {}
        extraction_errors += 1
    malware_status = _scan(path)
    if malware_status == MalwareStatus.MALICIOUS:
        raise DocumentValidationError("MALWARE_DETECTED", "The configured scanner rejected this file.")
    if malware_status == MalwareStatus.SCAN_ERROR:
        raise DocumentValidationError("MALWARE_SCAN_FAILED", "The configured malware scan did not complete safely.")
    findings = ["PDF_SIGNATURE_VALID", "PDF_PARSER_VALID", "NO_ACTIVE_CONTENT_DETECTED"]
    if malware_status == MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED:
        findings.append(MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED)
    extraction_status = "DIRECT_TEXT_PREVIEW" if preview else "NO_PREVIEW_TEXT"
    if extraction_errors:
        extraction_status = "PARTIAL_DIRECT_TEXT_PREVIEW" if preview else "PREVIEW_EXTRACTION_FAILED"
    return PdfValidationResult(
        page_count=page_count,
        preview_text=preview,
        extraction_status=extraction_status,
        detected_metadata=metadata,
        findings=findings,
        risk_flags=[],
        malware_status=malware_status,
    )
