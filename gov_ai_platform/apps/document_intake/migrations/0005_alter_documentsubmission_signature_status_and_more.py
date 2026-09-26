from django.db import migrations, models


SIGNATURE_CHOICES = [
    ("NOT_CHECKED", "Not checked"),
    ("NOT_PRESENT", "Not present"),
    ("SIGNATURE_PRESENT_UNVERIFIED", "Present — cryptographic verification not performed"),
    ("SIGNATURE_VERIFIED_VALID", "Cryptographically verified"),
    ("SIGNATURE_VERIFICATION_FAILED", "Cryptographic verification failed"),
    ("SIGNATURE_VERIFICATION_UNAVAILABLE", "Verification unavailable"),
]


def mark_legacy_signatures_not_checked(apps, schema_editor):
    submission_model = apps.get_model("document_intake", "DocumentSubmission")
    attempt_model = apps.get_model("document_intake", "DocumentValidationAttempt")
    submission_model.objects.filter(
        validator_version="stage3-legacy-pdf-validator-v1",
        signature_status="NOT_PRESENT",
    ).update(signature_status="NOT_CHECKED")
    attempt_model.objects.filter(
        validator_version="stage3-legacy-pdf-validator-v1",
        signature_status="NOT_PRESENT",
    ).update(signature_status="NOT_CHECKED")


class Migration(migrations.Migration):
    dependencies = [
        ("document_intake", "0004_alter_documentvalidationattempt_submission"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentsubmission",
            name="signature_status",
            field=models.CharField(
                choices=SIGNATURE_CHOICES,
                default="NOT_CHECKED",
                max_length=48,
            ),
        ),
        migrations.AlterField(
            model_name="documentvalidationattempt",
            name="signature_status",
            field=models.CharField(choices=SIGNATURE_CHOICES, max_length=48),
        ),
        migrations.RunPython(mark_legacy_signatures_not_checked, migrations.RunPython.noop),
    ]
