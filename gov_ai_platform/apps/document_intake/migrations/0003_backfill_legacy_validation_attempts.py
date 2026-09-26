from django.db import migrations


LEGACY_VALIDATOR_VERSION = "stage3-legacy-pdf-validator-v1"


def backfill_validation_attempts(apps, schema_editor):
    submission_model = apps.get_model("document_intake", "DocumentSubmission")
    attempt_model = apps.get_model("document_intake", "DocumentValidationAttempt")
    for submission in submission_model.objects.order_by("created_at").iterator(chunk_size=200):
        if submission.state == "VALIDATION_FAILED" and submission.page_count == 0:
            submission.page_count = None
        if submission.state == "VALIDATION_FAILED" and submission.extraction_status == "NOT_EXTRACTED":
            submission.extraction_status = "STOPPED_BY_SECURITY"
            submission.malware_status = "NOT_PERFORMED"
        submission.validator_version = submission.validator_version or LEGACY_VALIDATOR_VERSION
        submission.save(
            update_fields=("page_count", "extraction_status", "malware_status", "validator_version")
        )
        attempt_model.objects.get_or_create(
            submission_id=submission.pk,
            attempt_number=1,
            defaults={
                "requested_by_id": submission.submitted_by_id,
                "validator_version": submission.validator_version,
                "result_code": submission.safe_failure_code or "STRUCTURAL_VALIDATION_PASSED",
                "validation_state": submission.validation_state,
                "security_status": submission.security_status,
                "malware_status": submission.malware_status,
                "page_count": submission.page_count,
                "extraction_status": submission.extraction_status,
                "findings": submission.validation_findings,
                "risk_flags": submission.risk_flags,
                "signature_status": submission.signature_status,
                "signature_summary": submission.signature_summary,
                "external_uri_domains": submission.external_uri_domains,
                "safe_failure_code": submission.safe_failure_code,
                "safe_failure_reason": submission.safe_failure_reason,
                "internal_failure_detail": submission.internal_failure_detail,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("document_intake", "0002_documentsubmission_external_uri_domains_and_more")]

    operations = [migrations.RunPython(backfill_validation_attempts, migrations.RunPython.noop)]
