from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from apps.document_intake.forms import DocumentSubmissionForm
from apps.document_intake.models import (
    DocumentIngestionJob,
    DocumentSubmission,
    DocumentValidationAttempt,
    SecurityStatus,
    SignatureStatus,
    SourceProvenance,
    SubmissionState,
)
from apps.document_intake.services import DocumentIntakeError, revalidate_document, submit_document
from apps.document_intake.validation import (
    VALIDATOR_VERSION,
    DocumentValidationError,
    PdfInspection,
    validate_pdf,
)
from apps.governance.audit import verify_audit_chain
from apps.governance.constants import GENESIS_HASH, AuditAction
from apps.governance.models import GovernanceAuditChain, GovernanceAuditEvent
from apps.identity_access.models import AccessGrant, AccessRole, IASAuthorityScope, OrganizationUnit


def _add_annotation(writer: PdfWriter, annotation: DictionaryObject):
    page = writer.pages[0]
    annotations = page.get("/Annots")
    if annotations is None:
        annotations = ArrayObject()
        page[NameObject("/Annots")] = annotations
    annotations.append(writer._add_object(annotation))


def _action(action_type: str, **values) -> DictionaryObject:
    action = DictionaryObject({NameObject("/S"): NameObject(action_type)})
    for key, value in values.items():
        action[NameObject(f"/{key}")] = TextStringObject(value)
    return action


def feature_pdf_bytes(
    *,
    uri: bool = False,
    signature: bool = False,
    passive: bool = False,
    action_type: str = "",
    open_action_type: str = "",
    embedded: bool = False,
    file_attachment: bool = False,
) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=320, height=420)
    if uri:
        _add_annotation(writer, DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(20), NumberObject(20), NumberObject(180), NumberObject(45)]),
            NameObject("/A"): _action("/URI", URI="https://www.maharashtra.gov.in/"),
        }))
    if signature:
        signature_value = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
            NameObject("/Name"): TextStringObject("Test Government Signer"),
            NameObject("/M"): TextStringObject("D:20260925120000+05'30'"),
        }))
        widget = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject("GovernmentSignature"),
            NameObject("/Rect"): ArrayObject([NumberObject(20), NumberObject(60), NumberObject(180), NumberObject(90)]),
            NameObject("/V"): signature_value,
        })
        widget_reference = writer._add_object(widget)
        page = writer.pages[0]
        annotations = page.get("/Annots")
        if annotations is None:
            annotations = ArrayObject()
            page[NameObject("/Annots")] = annotations
        annotations.append(widget_reference)
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({
            NameObject("/SigFlags"): NumberObject(3),
            NameObject("/Fields"): ArrayObject([widget_reference]),
        }))
    if passive:
        _add_annotation(writer, DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Text"),
            NameObject("/Rect"): ArrayObject([NumberObject(20), NumberObject(100), NumberObject(40), NumberObject(120)]),
            NameObject("/Contents"): TextStringObject("Passive note"),
        }))
    if action_type:
        _add_annotation(writer, DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(20), NumberObject(140), NumberObject(180), NumberObject(165)]),
            NameObject("/A"): _action(action_type),
        }))
    if open_action_type:
        writer._root_object[NameObject("/OpenAction")] = writer._add_object(_action(open_action_type, JS="app.alert('x')"))
    if embedded:
        writer.add_attachment("inside.txt", b"blocked embedded payload")
    if file_attachment:
        stream = DecodedStreamObject()
        stream.set_data(b"blocked attachment payload")
        file_specification = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Filespec"),
            NameObject("/F"): TextStringObject("inside.txt"),
            NameObject("/EF"): DictionaryObject({NameObject("/F"): writer._add_object(stream)}),
        }))
        _add_annotation(writer, DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/FileAttachment"),
            NameObject("/Rect"): ArrayObject([NumberObject(20), NumberObject(180), NumberObject(40), NumberObject(200)]),
            NameObject("/FS"): file_specification,
        }))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class PdfClassifierTests(TestCase):
    def setUp(self):
        root = Path(settings.BASE_DIR) / "tmp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root, prefix="stage3b1-classifier-")
        self.override = override_settings(
            DOCUMENT_MALWARE_SCANNER="",
            DOCUMENT_MALWARE_SCANNER_EXECUTABLE="",
            DOCUMENT_MAX_UPLOAD_BYTES=1024 * 1024,
            DOCUMENT_MAX_PAGES=20,
            DOCUMENT_PREVIEW_PAGES=2,
            DOCUMENT_PREVIEW_CHARACTERS=1200,
        )
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.temporary.cleanup()

    def validate(self, content: bytes):
        path = Path(self.temporary.name) / "fixture.pdf"
        path.write_bytes(content)
        return validate_pdf(path, original_filename="fixture.pdf", declared_mime="application/pdf", department="Test")

    def blocked_code(self, content: bytes) -> str:
        with self.assertRaises(DocumentValidationError) as caught:
            self.validate(content)
        return caught.exception.code

    def test_uri_signature_and_passive_annotations_are_informational(self):
        result = self.validate(feature_pdf_bytes(uri=True, signature=True, passive=True))
        codes = {finding["code"] for finding in result.findings}
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.signature_status, SignatureStatus.PRESENT_UNVERIFIED)
        self.assertEqual(len(result.signature_summary), 1)
        self.assertEqual(result.external_uri_domains, ["www.maharashtra.gov.in"])
        self.assertTrue({"DIGITAL_SIGNATURE_PRESENT", "EXTERNAL_URI_PRESENT", "PASSIVE_ANNOTATION_PRESENT"} <= codes)
        self.assertFalse(any(finding["severity"] == "BLOCK" for finding in result.findings))

    def test_dangerous_actions_and_embedded_files_fail_closed(self):
        cases = {
            "javascript": (feature_pdf_bytes(action_type="/JavaScript"), "JAVASCRIPT_DETECTED"),
            "open-javascript": (feature_pdf_bytes(open_action_type="/JavaScript"), "JAVASCRIPT_DETECTED"),
            "launch": (feature_pdf_bytes(action_type="/Launch"), "LAUNCH_ACTION_DETECTED"),
            "submit-form": (feature_pdf_bytes(action_type="/SubmitForm"), "SUBMIT_FORM_DETECTED"),
            "import-data": (feature_pdf_bytes(action_type="/ImportData"), "IMPORT_DATA_DETECTED"),
            "unknown": (feature_pdf_bytes(action_type="/UnknownExecutable"), "UNSUPPORTED_ACTION_DETECTED"),
            "embedded": (feature_pdf_bytes(embedded=True), "EMBEDDED_FILE_DETECTED"),
            "file-attachment": (feature_pdf_bytes(file_attachment=True), "EMBEDDED_FILE_DETECTED"),
        }
        for label, (content, expected) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.blocked_code(content), expected)

    def test_safe_features_do_not_mask_javascript_and_malformed_stays_blocked(self):
        mixed = feature_pdf_bytes(uri=True, signature=True, passive=True, action_type="/JavaScript")
        self.assertEqual(self.blocked_code(mixed), "JAVASCRIPT_DETECTED")
        self.assertEqual(self.blocked_code(b"%PDF-broken"), "MALFORMED_PDF")


class Stage3B1WorkflowTests(TestCase):
    def setUp(self):
        root = Path(settings.BASE_DIR) / "tmp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root, prefix="stage3b1-workflow-")
        self.override = override_settings(
            DOCUMENT_QUARANTINE_ROOT=Path(self.temporary.name) / "Incoming" / "Officer_Uploads",
            DOCUMENT_APPROVED_ROOT=Path(self.temporary.name) / "Approved_Submissions",
            DOCUMENT_MALWARE_SCANNER="",
            DOCUMENT_MALWARE_SCANNER_EXECUTABLE="",
            DOCUMENT_MAX_UPLOAD_BYTES=1024 * 1024,
            DOCUMENT_MAX_PAGES=20,
            DOCUMENT_PREVIEW_PAGES=2,
            DOCUMENT_PREVIEW_CHARACTERS=1200,
        )
        self.override.enable()
        GovernanceAuditChain.objects.get_or_create(
            pk=1,
            defaults={"last_sequence": 0, "head_hash": GENESIS_HASH},
        )
        self.organization = OrganizationUnit.objects.create(code="stage3b1", name="Stage 3B.1")
        users = get_user_model()
        self.officer = users.objects.create_user("stage3b1-officer", password="TestOnlyPass-9321")
        self.ias = users.objects.create_user("stage3b1-ias", password="TestOnlyPass-9321")
        AccessGrant.objects.create(user=self.officer, organization=self.organization, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.ias, organization=self.organization, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.organization)

    def tearDown(self):
        self.override.disable()
        self.temporary.cleanup()

    def submit(self, actor, content: bytes, *, attested=False, name="fixture.pdf"):
        return submit_document(
            actor=actor,
            upload=SimpleUploadedFile(name, content, content_type="application/pdf"),
            organization=self.organization,
            claimed_identifier="TEST/2026/1",
            claimed_source_url="https://www.maharashtra.gov.in/example.pdf",
            source_context="Claimed official portal download.",
            ias_source_attested=attested,
            source_provenance=SourceProvenance.AUTHORITATIVE_SOURCE_VERIFIED,
            submission_reason="Stage 3B.1 regression.",
        )

    def test_role_does_not_change_byte_security_classification(self):
        dangerous = feature_pdf_bytes(action_type="/Launch")
        officer_bad = self.submit(self.officer, dangerous, name="officer-dangerous.pdf")
        ias_bad = self.submit(self.ias, dangerous, name="ias-dangerous.pdf")
        self.assertEqual(officer_bad.safe_failure_code, "LAUNCH_ACTION_DETECTED")
        self.assertEqual(ias_bad.safe_failure_code, officer_bad.safe_failure_code)

        safe = feature_pdf_bytes(uri=True, signature=True)
        officer_safe = self.submit(self.officer, safe, name="officer-safe.pdf")
        ias_safe = self.submit(self.ias, safe, attested=True, name="ias-safe.pdf")
        self.assertEqual(officer_safe.security_status, SecurityStatus.PASSED)
        self.assertEqual(ias_safe.security_status, SecurityStatus.PASSED)
        self.assertEqual(officer_safe.signature_status, ias_safe.signature_status)
        self.assertEqual(officer_safe.external_uri_domains, ias_safe.external_uri_domains)

    def test_provenance_is_server_derived_and_ias_attestation_is_explicit(self):
        organizations = OrganizationUnit.objects.filter(pk=self.organization.pk)
        officer_form = DocumentSubmissionForm(organizations=organizations, user=self.officer)
        ias_form = DocumentSubmissionForm(organizations=organizations, user=self.ias)
        self.assertNotIn("source_provenance", officer_form.fields)
        self.assertNotIn("ias_source_attested", officer_form.fields)
        self.assertIn("ias_source_attested", ias_form.fields)

        officer = self.submit(self.officer, feature_pdf_bytes(), attested=True, name="officer.pdf")
        ias = self.submit(self.ias, feature_pdf_bytes(passive=True), attested=True, name="ias.pdf")
        self.assertEqual(officer.submitter_role_snapshot, AccessRole.OFFICER)
        self.assertFalse(officer.ias_source_attested)
        self.assertEqual(officer.source_provenance, SourceProvenance.OFFICER_PROVIDED_UNVERIFIED)
        self.assertEqual(ias.submitter_role_snapshot, AccessRole.IAS_OFFICER)
        self.assertTrue(ias.ias_source_attested)
        self.assertEqual(ias.source_provenance, SourceProvenance.IAS_ATTESTED_OFFICIAL_COPY)

    def test_revalidation_preserves_history_audit_and_never_ingests(self):
        old_inspection = PdfInspection(
            page_count=1,
            findings=[{
                "code": "ANNOTATION_ACTION",
                "severity": "BLOCK",
                "classification": "LEGACY_FINDING",
                "user_title": "Earlier validator finding",
                "user_explanation": "Any annotation action was rejected.",
                "recommended_action": "Revalidate.",
                "technical_detail": "Legacy blanket annotation rule.",
                "evidence": {},
                "safe_to_continue": False,
            }],
            risk_flags=["ANNOTATION_ACTION"],
            extraction_status="STOPPED_BY_SECURITY",
        )
        with patch(
            "apps.document_intake.services.validate_pdf",
            side_effect=DocumentValidationError(
                "ACTIVE_CONTENT_DETECTED",
                "The earlier classifier rejected an annotation action.",
                inspection=old_inspection,
            ),
        ):
            submission = self.submit(
                self.officer,
                feature_pdf_bytes(uri=True, signature=True),
                name="legacy-failure.pdf",
            )
        old_failure_event = GovernanceAuditEvent.objects.get(
            action=AuditAction.DOCUMENT_VALIDATION_FAILED,
            correlation_id=submission.correlation_id,
        )
        self.assertEqual(DocumentValidationAttempt.objects.filter(submission=submission).count(), 1)

        result = revalidate_document(actor=self.officer, submission_id=submission.pk, expected_version=1)
        self.assertEqual(result.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertEqual(result.version, 2)
        self.assertEqual(result.validator_version, VALIDATOR_VERSION)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(DocumentValidationAttempt.objects.filter(submission=submission).count(), 2)
        self.assertTrue(GovernanceAuditEvent.objects.filter(event_id=old_failure_event.event_id).exists())
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_REVALIDATION_REQUESTED,
            correlation_id=submission.correlation_id,
        ).exists())
        self.assertTrue(GovernanceAuditEvent.objects.filter(
            action=AuditAction.DOCUMENT_VALIDATION_PASSED,
            correlation_id=submission.correlation_id,
        ).exists())
        self.assertFalse(DocumentIngestionJob.objects.filter(submission=submission).exists())
        self.assertTrue(verify_audit_chain().valid)
        with self.assertRaisesMessage(DocumentIntakeError, "changed"):
            revalidate_document(actor=self.officer, submission_id=submission.pk, expected_version=1)

    def test_failure_message_technical_disclosure_and_post_revalidation_ui(self):
        inspection = PdfInspection(
            page_count=1,
            findings=[{
                "code": "JAVASCRIPT_DETECTED",
                "severity": "BLOCK",
                "classification": "BLOCKED_ACTIVE_CONTENT",
                "user_title": "Document contains a blocked PDF feature",
                "user_explanation": "Executable JavaScript detected.",
                "recommended_action": "Upload a non-interactive copy.",
                "technical_detail": "Catalog/Names/JavaScript",
                "evidence": {"location": "Catalog/Names/JavaScript"},
                "safe_to_continue": False,
            }],
            risk_flags=["JAVASCRIPT_DETECTED"],
            extraction_status="STOPPED_BY_SECURITY",
        )
        with patch(
            "apps.document_intake.services.validate_pdf",
            side_effect=DocumentValidationError(
                "JAVASCRIPT_DETECTED",
                "Executable JavaScript detected.",
                inspection=inspection,
                technical_detail="JAVASCRIPT_DETECTED",
            ),
        ):
            submission = self.submit(self.officer, feature_pdf_bytes(uri=True), name="message-test.pdf")

        self.client.force_login(self.officer)
        officer_page = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(officer_page, "Document could not be accepted")
        self.assertContains(officer_page, "Upload a standard non-interactive copy")
        self.assertContains(officer_page, "Revalidate the same uploaded file")
        self.assertNotContains(officer_page, "Machine reason code")
        self.assertNotContains(officer_page, "JAVASCRIPT_DETECTED")

        self.client.force_login(self.ias)
        ias_page = self.client.get(reverse("document_intake:detail", args=[submission.pk]))
        self.assertContains(ias_page, "Show technical details")
        self.assertContains(ias_page, "JAVASCRIPT_DETECTED")
        failed_event = GovernanceAuditEvent.objects.get(
            action=AuditAction.DOCUMENT_VALIDATION_FAILED,
            correlation_id=submission.correlation_id,
        )
        audit_page = self.client.get(reverse("governance:audit_detail", args=[failed_event.event_id]))
        self.assertContains(audit_page, "Human-readable reason")
        self.assertContains(audit_page, "This PDF contains executable JavaScript")
        self.assertContains(audit_page, "Technical reason code")

        self.client.force_login(self.officer)
        response = self.client.post(
            reverse("document_intake:revalidate", args=[submission.pk]),
            {"expected_version": submission.version},
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.state, SubmissionState.PENDING_IAS_APPROVAL)
        self.assertFalse(DocumentIngestionJob.objects.filter(submission=submission).exists())
