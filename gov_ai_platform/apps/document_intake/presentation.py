from __future__ import annotations

from dataclasses import dataclass

from .models import MalwareStatus, SignatureStatus, SourceProvenance, SubmissionState


@dataclass(frozen=True)
class ValidationMessage:
    title: str
    explanation: str
    recommended_action: str


DEFAULT_FAILURE = ValidationMessage(
    "Document could not be accepted",
    "The PDF did not pass the bounded validation checks.",
    "Obtain a standard PDF from the official source and try again.",
)

FAILURE_MESSAGES = {
    "JAVASCRIPT_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains executable JavaScript.",
        "Upload a standard non-interactive copy from the official government portal.",
    ),
    "LAUNCH_ACTION_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains an action that can open a file or application.",
        "Upload a non-interactive copy without launch actions.",
    ),
    "EMBEDDED_FILE_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains another file inside it.",
        "Upload the original government PDF without embedded attachments.",
    ),
    "FILE_ATTACHMENT_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains a file attachment.",
        "Upload the original government PDF without attachments.",
    ),
    "SUBMIT_FORM_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF can submit form data to another destination.",
        "Upload a flattened, non-interactive official copy.",
    ),
    "IMPORT_DATA_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF can import data from another source.",
        "Upload a standard non-interactive official copy.",
    ),
    "RENDITION_ACTION_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains unsupported multimedia behavior.",
        "Upload a standard PDF without multimedia actions.",
    ),
    "UNSUPPORTED_ACTION_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "This PDF contains an action that the security validator cannot safely classify.",
        "Upload a standard non-interactive copy from the official source.",
    ),
    "MALFORMED_PDF": ValidationMessage(
        "Document could not be read",
        "The PDF structure is damaged or unsupported.",
        "Download the document again from the official source and retry.",
    ),
    "ENCRYPTED_PDF": ValidationMessage(
        "Password-protected PDF not accepted",
        "The system cannot safely inspect an encrypted or password-protected PDF.",
        "Upload an unencrypted official copy.",
    ),
    "PAGE_LIMIT_EXCEEDED": ValidationMessage(
        "Document is too long for bounded validation",
        "The PDF exceeds the configured page limit.",
        "Confirm that this is the intended document or contact an administrator.",
    ),
    "ACTIVE_CONTENT_DETECTED": ValidationMessage(
        "Document could not be accepted",
        "The earlier validator reported active PDF content.",
        "Use Revalidate to apply the current feature-aware validator to the same bytes.",
    ),
    "EXACT_PENDING_DUPLICATE": ValidationMessage(
        "Document is already pending",
        "The same file bytes already exist in an active submission.",
        "Open the existing submission instead of uploading another copy.",
    ),
    "EXACT_CORPUS_DUPLICATE": ValidationMessage(
        "Document already exists in the corpus",
        "The same file bytes are already admitted.",
        "No new upload is required.",
    ),
}


def failure_message_for_code(code: str, fallback: str = "") -> ValidationMessage:
    mapped = FAILURE_MESSAGES.get(code)
    if mapped:
        return mapped
    if fallback:
        return ValidationMessage(DEFAULT_FAILURE.title, fallback, DEFAULT_FAILURE.recommended_action)
    return DEFAULT_FAILURE


def _normalize_finding(value) -> dict:
    if isinstance(value, dict):
        return {
            "code": str(value.get("code", "UNKNOWN_FINDING")),
            "severity": str(value.get("severity", "INFO")),
            "classification": str(value.get("classification", "SAFE_PASSIVE")),
            "user_title": str(value.get("user_title", "Validation finding")),
            "user_explanation": str(value.get("user_explanation", "")),
            "recommended_action": str(value.get("recommended_action", "")),
            "technical_detail": str(value.get("technical_detail", "")),
            "evidence": value.get("evidence", {}) if isinstance(value.get("evidence", {}), dict) else {},
        }
    code = str(value)
    mapped = failure_message_for_code(code)
    severity = "BLOCK" if code.startswith(("BLOCKED_", "ACTIVE_", "JAVASCRIPT", "EMBEDDED")) else "INFO"
    return {
        "code": code,
        "severity": severity,
        "classification": "LEGACY_FINDING",
        "user_title": mapped.title if severity == "BLOCK" else "Earlier validator finding",
        "user_explanation": mapped.explanation if severity == "BLOCK" else "Recorded by an earlier validator version.",
        "recommended_action": mapped.recommended_action if severity == "BLOCK" else "",
        "technical_detail": code,
        "evidence": {},
    }


def validation_presentation(submission, *, show_technical: bool) -> dict:
    findings = [_normalize_finding(value) for value in (submission.validation_findings or [])]
    grouped = {severity: [] for severity in ("BLOCK", "REVIEW", "WARNING", "INFO")}
    for finding in findings:
        grouped.setdefault(finding["severity"], []).append(finding)
    failure = failure_message_for_code(submission.safe_failure_code, submission.safe_failure_reason)
    if submission.state != SubmissionState.VALIDATION_FAILED:
        failure = None
    signature_count = len(submission.signature_summary or [])
    signature_labels = {
        SignatureStatus.NOT_CHECKED: "Not checked",
        SignatureStatus.NOT_PRESENT: "No digital signature found",
        SignatureStatus.PRESENT_UNVERIFIED: "Present — cryptographic verification not performed",
        SignatureStatus.VERIFIED_VALID: "Cryptographically verified",
        SignatureStatus.VERIFICATION_FAILED: "Present — verification failed",
        SignatureStatus.VERIFICATION_UNAVAILABLE: "Present — verification unavailable",
    }
    malware_labels = {
        MalwareStatus.NOT_PERFORMED: "Not performed because validation stopped earlier",
        MalwareStatus.CLEAN: "Scanner reported no malware",
        MalwareStatus.MALWARE_SCAN_NOT_CONFIGURED: "Malware scan unavailable in this local deployment",
        MalwareStatus.MALICIOUS: "Malware detected",
        MalwareStatus.SCAN_ERROR: "Malware scan did not complete",
    }
    extraction_labels = {
        "NOT_STARTED": "Preview extraction has not started",
        "NOT_EXTRACTED": "Preview was not generated because validation stopped processing",
        "STOPPED_BY_SECURITY": "Preview was not generated because security validation stopped processing",
        "PREVIEW_EXTRACTION_STOPPED": "Preview extraction stopped at the validation time limit",
        "DIRECT_TEXT_PREVIEW": "Bounded text preview completed",
        "PARTIAL_DIRECT_TEXT_PREVIEW": "A partial bounded text preview was generated",
        "NO_PREVIEW_TEXT": "No directly extractable preview text was found",
        "PREVIEW_EXTRACTION_FAILED": "Preview extraction could not be completed",
    }
    if submission.source_provenance == SourceProvenance.AUTHORITATIVE_SOURCE_VERIFIED:
        source_verification = "Independently verified"
    elif submission.source_provenance == SourceProvenance.IAS_ATTESTED_OFFICIAL_COPY:
        source_verification = "IAS-attested copy — not independently verified"
    else:
        source_verification = "Not independently verified"
    external_link_count = next(
        (
            int(finding["evidence"].get("count", 0))
            for finding in findings
            if finding["code"] == "EXTERNAL_URI_PRESENT"
        ),
        len(submission.external_uri_domains or []),
    )
    page_count = submission.page_count
    return {
        "failure": failure,
        "findings": grouped,
        "show_technical": show_technical,
        "page_count": f"{page_count} page{'s' if page_count != 1 else ''}" if page_count is not None else "Not checked",
        "file_structure": "Safe" if submission.security_status == "PASSED" else "Issue found",
        "signature_count": signature_count,
        "signature_label": signature_labels.get(submission.signature_status, "Not checked"),
        "external_link_count": external_link_count,
        "malware_label": malware_labels.get(submission.malware_status, "Not checked"),
        "extraction_label": extraction_labels.get(submission.extraction_status, "Not checked"),
        "source_verification": source_verification,
        "corpus_admission": submission.get_state_display(),
    }
