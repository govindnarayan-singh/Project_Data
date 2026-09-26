from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from pypdf import PdfReader

from knowledge_engine.extraction.text_quality import normalize_marathi_text
from knowledge_engine.metadata.extractor import extract_metadata

from .models import MalwareStatus, SignatureStatus


VALIDATOR_VERSION = "stage3b1-pdf-classifier-v2"
BLOCK = "BLOCK"
REVIEW = "REVIEW"
WARNING = "WARNING"
INFO = "INFO"

SAFE_PASSIVE = "SAFE_PASSIVE"
SAFE_SIGNATURE = "SAFE_SIGNATURE"
SAFE_EXTERNAL_LINK = "SAFE_EXTERNAL_LINK"
BLOCKED_ACTIVE_CONTENT = "BLOCKED_ACTIVE_CONTENT"
BLOCKED_EMBEDDED_FILE = "BLOCKED_EMBEDDED_FILE"
BLOCKED_UNSUPPORTED_ACTION = "BLOCKED_UNSUPPORTED_ACTION"

PASSIVE_ANNOTATIONS = {
    "/Caret", "/Circle", "/FreeText", "/Highlight", "/Ink", "/Line",
    "/Popup", "/Polygon", "/PolyLine", "/Square", "/Squiggly", "/Stamp",
    "/StrikeOut", "/Text", "/Underline",
}
SAFE_NAMED_NAVIGATION = {
    "/FirstPage", "/GoBack", "/GoForward", "/LastPage", "/NextPage", "/PrevPage",
}
BLOCKED_ACTION_CODES = {
    "/JavaScript": ("JAVASCRIPT_DETECTED", BLOCKED_ACTIVE_CONTENT, "Executable JavaScript detected"),
    "/Launch": ("LAUNCH_ACTION_DETECTED", BLOCKED_ACTIVE_CONTENT, "File or application launch action detected"),
    "/SubmitForm": ("SUBMIT_FORM_DETECTED", BLOCKED_ACTIVE_CONTENT, "Form submission action detected"),
    "/ImportData": ("IMPORT_DATA_DETECTED", BLOCKED_ACTIVE_CONTENT, "External data import action detected"),
    "/Rendition": ("RENDITION_ACTION_DETECTED", BLOCKED_UNSUPPORTED_ACTION, "Unsupported multimedia action detected"),
}
BLOCKER_PRIORITY = (
    "JAVASCRIPT_DETECTED", "LAUNCH_ACTION_DETECTED", "EMBEDDED_FILE_DETECTED",
    "FILE_ATTACHMENT_DETECTED", "SUBMIT_FORM_DETECTED", "IMPORT_DATA_DETECTED",
    "RENDITION_ACTION_DETECTED", "UNSUPPORTED_ACTION_DETECTED",
)


def _bounded_text(value, limit: int = 160) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split())[:limit]


def _dereference(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _finding(
    code: str,
    severity: str,
    classification: str,
    user_title: str,
    user_explanation: str,
    recommended_action: str = "",
    technical_detail: str = "",
    evidence: dict | None = None,
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "classification": classification,
        "user_title": user_title,
        "user_explanation": user_explanation,
        "recommended_action": recommended_action,
        "technical_detail": _bounded_text(technical_detail, 500),
        "evidence": evidence or {},
        "safe_to_continue": severity != BLOCK,
    }


@dataclass
class PdfInspection:
    page_count: int | None = None
    findings: list[dict] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    signature_status: str = SignatureStatus.NOT_CHECKED
    signature_summary: list[dict] = field(default_factory=list)
    external_uri_domains: list[str] = field(default_factory=list)
    malware_status: str = MalwareStatus.NOT_PERFORMED
    extraction_status: str = "NOT_STARTED"


class DocumentValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        inspection: PdfInspection | None = None,
        findings: list[dict] | None = None,
        technical_detail: str = "",
    ):
        self.code = code
        self.safe_message = message
        self.inspection = inspection or PdfInspection()
        if findings is not None:
            self.inspection.findings = findings
        self.findings = self.inspection.findings
        self.technical_detail = _bounded_text(technical_detail, 1000)
        super().__init__(message)


@dataclass(frozen=True)
class PdfValidationResult:
    page_count: int
    preview_text: str
    extraction_status: str
    detected_metadata: dict
    findings: list[dict]
    risk_flags: list[str]
    malware_status: str
    signature_status: str
    signature_summary: list[dict]
    external_uri_domains: list[str]
    validator_version: str = VALIDATOR_VERSION


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


class _FeatureInspector:
    def __init__(self, reader: PdfReader, page_count: int, *, deadline: float):
        self.reader = reader
        self.deadline = deadline
        self.result = PdfInspection(
            page_count=page_count,
            signature_status=SignatureStatus.NOT_PRESENT,
            extraction_status="STOPPED_BY_SECURITY",
        )
        self._signature_keys: set[str] = set()
        self._passive_counts: dict[str, int] = {}
        self._external_domains: set[str] = set()
        self._external_link_count = 0
        self._feature_count = 0
        self._field_keys: set[tuple] = set()

    def _guard(self):
        self._feature_count += 1
        if self._feature_count > 5000:
            raise DocumentValidationError(
                "PDF_FEATURE_LIMIT_EXCEEDED",
                "The PDF contains too many interactive structures for bounded validation.",
                inspection=self.result,
            )
        if time.monotonic() > self.deadline:
            raise DocumentValidationError(
                "VALIDATION_TIMEOUT",
                "PDF validation exceeded the configured time limit.",
                inspection=self.result,
            )

    def add_blocker(self, code: str, classification: str, title: str, detail: str, evidence: dict | None = None):
        if code not in self.result.risk_flags:
            self.result.risk_flags.append(code)
            self.result.findings.append(_finding(
                code,
                BLOCK,
                classification,
                "Document contains a blocked PDF feature",
                title + ".",
                "Obtain a standard non-interactive PDF from the official source and upload that copy.",
                detail,
                evidence,
            ))

    def add_signature(self, value, *, field_name: str, location: str):
        signature = _dereference(value) if value else {}
        summary = {
            "field_name": _bounded_text(field_name, 120) or "Unnamed signature field",
            "signer_name": _bounded_text(signature.get("/Name") if hasattr(signature, "get") else "", 120),
            "signing_time": _bounded_text(signature.get("/M") if hasattr(signature, "get") else "", 80),
            "filter": _bounded_text(signature.get("/Filter") if hasattr(signature, "get") else "", 80),
            "subfilter": _bounded_text(signature.get("/SubFilter") if hasattr(signature, "get") else "", 80),
            "certificate_subject_summary": "",
            "location": location,
        }
        key = json.dumps({name: value for name, value in summary.items() if name != "location"}, sort_keys=True)
        if key in self._signature_keys:
            return
        self._signature_keys.add(key)
        self.result.signature_summary.append(summary)

    def _classify_action(self, value, *, location: str, automatic: bool = False, visited: set[tuple] | None = None, depth: int = 0):
        self._guard()
        if depth > 16:
            self.add_blocker(
                "UNSUPPORTED_ACTION_DETECTED",
                BLOCKED_UNSUPPORTED_ACTION,
                "An excessively nested action chain was detected",
                f"{location} exceeds the bounded action-chain depth.",
                {"location": location},
            )
            return
        action = _dereference(value)
        if not action:
            return
        if not hasattr(action, "get"):
            self.add_blocker(
                "UNSUPPORTED_ACTION_DETECTED",
                BLOCKED_UNSUPPORTED_ACTION,
                "An unsupported executable action was detected",
                f"{location} is not a safely classifiable action dictionary.",
                {"location": location},
            )
            return
        reference = getattr(action, "indirect_reference", None)
        key = (
            getattr(reference, "idnum", None),
            getattr(reference, "generation", None),
        ) if reference is not None else ("direct", id(action))
        visited = visited or set()
        if key in visited:
            self.add_blocker(
                "UNSUPPORTED_ACTION_DETECTED",
                BLOCKED_UNSUPPORTED_ACTION,
                "A cyclic action chain was detected",
                f"{location} repeats an earlier action object.",
                {"location": location},
            )
            return
        visited.add(key)
        action_type = str(action.get("/S", ""))
        if action_type == "/URI" and not automatic:
            uri = _bounded_text(action.get("/URI", ""), 1000)
            parsed = urlsplit(uri)
            if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname:
                self._external_domains.add(parsed.hostname.casefold())
                self._external_link_count += 1
            else:
                self.add_blocker(
                    "UNSUPPORTED_ACTION_DETECTED",
                    BLOCKED_UNSUPPORTED_ACTION,
                    "An unsupported external-link action was detected",
                    f"{location} uses a non-HTTP(S) or malformed URI action.",
                    {"location": location, "action_type": action_type},
                )
        elif action_type == "/GoTo":
            self._passive_counts["INTERNAL_NAVIGATION"] = self._passive_counts.get("INTERNAL_NAVIGATION", 0) + 1
        elif action_type == "/Named" and str(action.get("/N", "")) in SAFE_NAMED_NAVIGATION:
            self._passive_counts["NAMED_NAVIGATION"] = self._passive_counts.get("NAMED_NAVIGATION", 0) + 1
        elif action_type in BLOCKED_ACTION_CODES:
            code, classification, title = BLOCKED_ACTION_CODES[action_type]
            self.add_blocker(
                code,
                classification,
                title,
                f"{location} contains action type {action_type}.",
                {"location": location, "action_type": action_type},
            )
        else:
            self.add_blocker(
                "UNSUPPORTED_ACTION_DETECTED",
                BLOCKED_UNSUPPORTED_ACTION,
                "An unsupported executable action was detected",
                f"{location} contains unapproved action type {action_type or 'UNKNOWN'}.",
                {"location": location, "action_type": action_type or "UNKNOWN"},
            )
        next_action = action.get("/Next")
        if next_action:
            chain = _dereference(next_action)
            for index, chained in enumerate(chain if isinstance(chain, (list, tuple)) else [chain], 1):
                self._classify_action(
                    chained,
                    location=f"{location}/Next[{index}]",
                    automatic=automatic,
                    visited=set(visited),
                    depth=depth + 1,
                )

    def _classify_additional_actions(self, value, *, location: str):
        self._guard()
        additional = _dereference(value)
        if not hasattr(additional, "items"):
            self.add_blocker(
                "UNSUPPORTED_ACTION_DETECTED",
                BLOCKED_UNSUPPORTED_ACTION,
                "An unsupported automatic action was detected",
                f"{location} could not be safely classified.",
                {"location": location},
            )
            return
        for trigger, action in additional.items():
            self._classify_action(action, location=f"{location}/{trigger}", automatic=True)

    def _classify_file_specs(self, value, *, location: str):
        resolved = _dereference(value)
        for index, reference in enumerate(resolved if isinstance(resolved, (list, tuple)) else [resolved], 1):
            self._guard()
            file_spec = _dereference(reference)
            if hasattr(file_spec, "get") and file_spec.get("/EF"):
                self.add_blocker(
                    "EMBEDDED_FILE_DETECTED",
                    BLOCKED_EMBEDDED_FILE,
                    "An embedded file stream was detected",
                    f"{location}[{index}] contains an /EF embedded stream.",
                    {"location": f"{location}[{index}]/EF"},
                )

    def _field(self, reference, *, inherited_type: str = "", location: str = "AcroForm", depth: int = 0):
        self._guard()
        if depth > 32:
            raise DocumentValidationError(
                "PDF_FEATURE_LIMIT_EXCEEDED",
                "The PDF form structure is too deeply nested for bounded validation.",
                inspection=self.result,
            )
        item = _dereference(reference)
        if not hasattr(item, "get"):
            return
        indirect = getattr(item, "indirect_reference", None)
        key = (
            getattr(indirect, "idnum", None),
            getattr(indirect, "generation", None),
        ) if indirect is not None else ("direct", id(item))
        if key in self._field_keys:
            return
        self._field_keys.add(key)
        field_type = str(item.get("/FT") or inherited_type or "")
        field_name = _bounded_text(item.get("/T", ""), 120)
        if field_type == "/Sig":
            self.add_signature(item.get("/V"), field_name=field_name, location=location)
        if item.get("/A"):
            self._classify_action(item.get("/A"), location=f"{location}/A")
        if item.get("/AA"):
            self._classify_additional_actions(item.get("/AA"), location=f"{location}/AA")
        for index, child in enumerate(_dereference(item.get("/Kids", [])) or [], 1):
            self._field(child, inherited_type=field_type, location=f"{location}/Kids[{index}]", depth=depth + 1)

    def inspect(self) -> PdfInspection:
        try:
            root = _dereference(self.reader.trailer["/Root"])
            names = _dereference(root.get("/Names", {}))
            if hasattr(names, "get") and names.get("/EmbeddedFiles"):
                self.add_blocker(
                    "EMBEDDED_FILE_DETECTED",
                    BLOCKED_EMBEDDED_FILE,
                    "An embedded file was detected",
                    "The catalog contains an /EmbeddedFiles name tree.",
                    {"location": "Catalog/Names/EmbeddedFiles"},
                )
            if hasattr(names, "get") and names.get("/JavaScript"):
                self.add_blocker(
                    "JAVASCRIPT_DETECTED",
                    BLOCKED_ACTIVE_CONTENT,
                    "Executable JavaScript was detected",
                    "The catalog contains a /JavaScript name tree.",
                    {"location": "Catalog/Names/JavaScript"},
                )
            open_action = root.get("/OpenAction")
            if open_action:
                resolved = _dereference(open_action)
                if hasattr(resolved, "get"):
                    self._classify_action(resolved, location="Catalog/OpenAction", automatic=True)
                else:
                    self._passive_counts["DOCUMENT_DESTINATION"] = 1
            if root.get("/AA"):
                self._classify_additional_actions(root.get("/AA"), location="Catalog/AA")
            if root.get("/AF"):
                self._classify_file_specs(root.get("/AF"), location="Catalog/AF")
            acro_form = _dereference(root.get("/AcroForm", {}))
            if hasattr(acro_form, "get"):
                for index, field_reference in enumerate(_dereference(acro_form.get("/Fields", [])) or [], 1):
                    self._field(field_reference, location=f"AcroForm/Fields[{index}]")

            for page_number, page in enumerate(self.reader.pages, 1):
                self._guard()
                page_object = _dereference(page)
                if page_object.get("/AA"):
                    self._classify_additional_actions(page_object.get("/AA"), location=f"Page[{page_number}]/AA")
                if page_object.get("/AF"):
                    self._classify_file_specs(page_object.get("/AF"), location=f"Page[{page_number}]/AF")
                annotations = _dereference(page_object.get("/Annots", [])) or []
                for annotation_number, reference in enumerate(annotations, 1):
                    self._guard()
                    annotation = _dereference(reference)
                    if not hasattr(annotation, "get"):
                        continue
                    location = f"Page[{page_number}]/Annots[{annotation_number}]"
                    subtype = str(annotation.get("/Subtype", ""))
                    parent = _dereference(annotation.get("/Parent", {}))
                    parent_get = parent.get if hasattr(parent, "get") else lambda *_: None
                    field_type = str(annotation.get("/FT") or parent_get("/FT") or "")
                    field_name = _bounded_text(annotation.get("/T") or parent_get("/T") or "", 120)
                    if subtype == "/Widget" and field_type == "/Sig":
                        self.add_signature(
                            annotation.get("/V") or parent_get("/V"),
                            field_name=field_name,
                            location=location,
                        )
                    elif subtype == "/FileAttachment":
                        self.add_blocker(
                            "FILE_ATTACHMENT_DETECTED",
                            BLOCKED_EMBEDDED_FILE,
                            "A file attachment annotation was detected",
                            f"{location} is a /FileAttachment annotation.",
                            {"location": location, "subtype": subtype},
                        )
                    elif subtype in PASSIVE_ANNOTATIONS or (subtype and subtype not in {"/Link", "/Widget"}):
                        self._passive_counts[subtype or "UNKNOWN_PASSIVE"] = self._passive_counts.get(subtype or "UNKNOWN_PASSIVE", 0) + 1
                    if annotation.get("/A"):
                        self._classify_action(annotation.get("/A"), location=f"{location}/A")
                    if annotation.get("/AA"):
                        self._classify_additional_actions(annotation.get("/AA"), location=f"{location}/AA")
                    file_spec = _dereference(annotation.get("/FS", {}))
                    if hasattr(file_spec, "get") and file_spec.get("/EF"):
                        self.add_blocker(
                            "EMBEDDED_FILE_DETECTED",
                            BLOCKED_EMBEDDED_FILE,
                            "An embedded file stream was detected",
                            f"{location}/FS contains an /EF embedded stream.",
                            {"location": f"{location}/FS/EF"},
                        )
        except DocumentValidationError:
            raise
        except Exception as exc:
            raise DocumentValidationError(
                "MALFORMED_PDF",
                "The PDF structure could not be inspected safely.",
                inspection=self.result,
                technical_detail=type(exc).__name__,
            ) from exc

        if self.result.signature_summary:
            self.result.signature_status = SignatureStatus.PRESENT_UNVERIFIED
            self.result.findings.append(_finding(
                "DIGITAL_SIGNATURE_PRESENT",
                INFO,
                SAFE_SIGNATURE,
                "Digital signature fields found",
                f"{len(self.result.signature_summary)} digital signature field(s) were detected.",
                "Treat signature presence as evidence only until cryptographic verification is available.",
                "AcroForm /Sig fields or /Widget annotations were present; signature bytes were not exported.",
                {"count": len(self.result.signature_summary), "verification": SignatureStatus.PRESENT_UNVERIFIED},
            ))
        if self._external_domains:
            self.result.external_uri_domains = sorted(self._external_domains)[:20]
            self.result.findings.append(_finding(
                "EXTERNAL_URI_PRESENT",
                INFO,
                SAFE_EXTERNAL_LINK,
                "External website links found",
                f"{self._external_link_count} external website link(s) were detected.",
                "No link was opened or fetched during validation.",
                "User-triggered HTTP(S) /URI actions were classified as non-executable external links.",
                {"count": self._external_link_count, "domains": self.result.external_uri_domains},
            ))
        if self._passive_counts:
            self.result.findings.append(_finding(
                "PASSIVE_ANNOTATION_PRESENT",
                INFO,
                SAFE_PASSIVE,
                "Passive PDF annotations found",
                "The PDF contains non-executable annotations or navigation.",
                "No action is required.",
                "Passive annotation subtypes and internal navigation were classified separately from executable actions.",
                {"count": sum(self._passive_counts.values()), "types": sorted(self._passive_counts)[:20]},
            ))
        return self.result


def _primary_blocker(risk_flags: list[str]) -> str:
    return next((code for code in BLOCKER_PRIORITY if code in risk_flags), "UNSUPPORTED_ACTION_DETECTED")


def validate_pdf(path: Path, *, original_filename: str, declared_mime: str, department: str) -> PdfValidationResult:
    started = time.monotonic()
    inspection = PdfInspection()
    if Path(original_filename).suffix.casefold() != ".pdf":
        raise DocumentValidationError("UNSUPPORTED_EXTENSION", "Only PDF files are accepted.", inspection=inspection)
    if declared_mime.casefold().split(";", 1)[0].strip() != "application/pdf":
        raise DocumentValidationError("MIME_MISMATCH", "The upload must be a PDF file.", inspection=inspection)
    size = path.stat().st_size
    if size == 0:
        raise DocumentValidationError("EMPTY_FILE", "The uploaded file is empty.", inspection=inspection)
    if size > settings.DOCUMENT_MAX_UPLOAD_BYTES:
        raise DocumentValidationError("FILE_TOO_LARGE", "The PDF exceeds the configured upload limit.", inspection=inspection)
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise DocumentValidationError("INVALID_PDF_SIGNATURE", "The file is not a valid PDF.", inspection=inspection)
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise DocumentValidationError(
            "MALFORMED_PDF", "The PDF cannot be parsed safely.", inspection=inspection, technical_detail=type(exc).__name__
        ) from exc
    if reader.is_encrypted:
        raise DocumentValidationError("ENCRYPTED_PDF", "Password-protected PDFs are not accepted.", inspection=inspection)
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise DocumentValidationError(
            "MALFORMED_PDF", "The PDF page structure cannot be read safely.", inspection=inspection, technical_detail=type(exc).__name__
        ) from exc
    inspection.page_count = page_count
    if page_count == 0:
        raise DocumentValidationError("ZERO_PAGE_PDF", "The PDF contains no pages.", inspection=inspection)
    if page_count > settings.DOCUMENT_MAX_PAGES:
        raise DocumentValidationError("PAGE_LIMIT_EXCEEDED", "The PDF exceeds the configured page limit.", inspection=inspection)

    inspection = _FeatureInspector(
        reader,
        page_count,
        deadline=started + settings.DOCUMENT_VALIDATION_TIMEOUT_SECONDS,
    ).inspect()
    if inspection.risk_flags:
        code = _primary_blocker(inspection.risk_flags)
        raise DocumentValidationError(
            code,
            next(f["user_explanation"] for f in inspection.findings if f["code"] == code),
            inspection=inspection,
            technical_detail="; ".join(inspection.risk_flags),
        )

    malware_status = _scan(path)
    inspection.malware_status = malware_status
    if malware_status == MalwareStatus.MALICIOUS:
        finding = _finding(
            "MALWARE_DETECTED", BLOCK, BLOCKED_ACTIVE_CONTENT, "Malware was detected",
            "The configured malware scanner rejected this file.",
            "Obtain a clean original from the official source.", "The configured scanner returned a malicious result.",
        )
        inspection.findings.append(finding)
        inspection.risk_flags.append("MALWARE_DETECTED")
        raise DocumentValidationError("MALWARE_DETECTED", finding["user_explanation"], inspection=inspection)
    if malware_status == MalwareStatus.SCAN_ERROR:
        finding = _finding(
            "MALWARE_SCAN_FAILED", BLOCK, BLOCKED_UNSUPPORTED_ACTION, "Malware scan did not complete",
            "The file could not be accepted because the configured malware scan failed.",
            "Try again or ask an administrator to check the local scanner.", "The configured scanner returned an operational error.",
        )
        inspection.findings.append(finding)
        inspection.risk_flags.append("MALWARE_SCAN_FAILED")
        raise DocumentValidationError("MALWARE_SCAN_FAILED", finding["user_explanation"], inspection=inspection)
    if malware_status == MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED:
        inspection.findings.append(_finding(
            MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED,
            WARNING,
            SAFE_PASSIVE,
            "Malware scan unavailable",
            "Malware scanning is unavailable in this local deployment.",
            "Use the deployment malware-scanning control when available.",
            MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED,
        ))

    preview_parts: list[str] = []
    extraction_errors = 0
    for page in reader.pages[: settings.DOCUMENT_PREVIEW_PAGES]:
        try:
            preview_parts.append(page.extract_text() or "")
        except Exception:
            extraction_errors += 1
        if time.monotonic() - started > settings.DOCUMENT_VALIDATION_TIMEOUT_SECONDS:
            inspection.extraction_status = "PREVIEW_EXTRACTION_STOPPED"
            raise DocumentValidationError(
                "VALIDATION_TIMEOUT", "PDF validation exceeded the configured time limit.", inspection=inspection
            )
    preview = normalize_marathi_text("\n".join(preview_parts))[: settings.DOCUMENT_PREVIEW_CHARACTERS]
    try:
        metadata = extract_metadata(preview, department, original_filename) if preview else {}
        metadata = json.loads(json.dumps(metadata, default=str))
    except Exception:
        metadata = {}
        extraction_errors += 1
    extraction_status = "DIRECT_TEXT_PREVIEW" if preview else "NO_PREVIEW_TEXT"
    if extraction_errors:
        extraction_status = "PARTIAL_DIRECT_TEXT_PREVIEW" if preview else "PREVIEW_EXTRACTION_FAILED"
    inspection.extraction_status = extraction_status
    inspection.findings.extend([
        _finding(
            "PDF_STRUCTURE_VALID", INFO, SAFE_PASSIVE, "PDF structure is readable",
            f"The PDF contains {page_count} readable page(s).", "No action is required.",
            "The parser opened the catalog and page tree within configured limits.", {"page_count": page_count},
        ),
        _finding(
            "NO_BLOCKED_ACTIVE_CONTENT", INFO, SAFE_PASSIVE, "No blocked active content found",
            "No JavaScript, launch action, embedded file, or unsupported executable action was detected.",
            "The document may proceed to provenance and IAS review.",
            "The deterministic feature classifier produced no BLOCK finding.",
        ),
    ])
    return PdfValidationResult(
        page_count=page_count,
        preview_text=preview,
        extraction_status=extraction_status,
        detected_metadata=metadata,
        findings=inspection.findings,
        risk_flags=[],
        malware_status=malware_status,
        signature_status=inspection.signature_status,
        signature_summary=inspection.signature_summary,
        external_uri_domains=inspection.external_uri_domains,
    )
