from django.db import migrations, models


DOCUMENT_ACTIONS = [
    "ACCESS_REQUEST_SUBMITTED", "ACCESS_REQUEST_APPROVED", "ACCESS_REQUEST_REJECTED",
    "ACCESS_GRANT_CREATED", "ACCESS_GRANT_REACTIVATED", "ACCESS_GRANT_SUSPENDED",
    "ACCESS_GRANT_REVOKED", "IAS_BOOTSTRAPPED", "ACTIVATION_CONSUMED",
    "APPROVAL_CASE_CREATED", "APPROVAL_CASE_APPROVED", "APPROVAL_CASE_REJECTED",
    "ACCESS_REQUEST_STATE_IMPORTED", "DOCUMENT_SUBMITTED", "DOCUMENT_VALIDATION_FAILED",
    "DOCUMENT_READY_FOR_APPROVAL", "DOCUMENT_APPROVED", "DOCUMENT_REJECTED",
    "DOCUMENT_INGESTION_QUEUED", "DOCUMENT_INGESTION_RETRIED",
    "DOCUMENT_INGESTION_SUCCEEDED", "DOCUMENT_INGESTION_FAILED",
]


class Migration(migrations.Migration):
    dependencies = [("governance", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(model_name="governanceauditevent", name="gov_audit_category_valid"),
        migrations.RemoveConstraint(model_name="governanceauditevent", name="gov_audit_action_valid"),
        migrations.AlterField(
            model_name="governanceauditevent",
            name="category",
            field=models.CharField(
                choices=[
                    ("IDENTITY_ACCESS", "Identity and access"), ("APPROVAL", "Approval"),
                    ("AUTHORIZATION", "Authorization"), ("SECURITY", "Security"),
                    ("SYSTEM", "System"), ("DOCUMENT", "Document governance"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="governanceauditevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("ACCESS_REQUEST_SUBMITTED", "Access request submitted"),
                    ("ACCESS_REQUEST_APPROVED", "Access request approved"),
                    ("ACCESS_REQUEST_REJECTED", "Access request rejected"),
                    ("ACCESS_GRANT_CREATED", "Access grant created"),
                    ("ACCESS_GRANT_REACTIVATED", "Access grant reactivated"),
                    ("ACCESS_GRANT_SUSPENDED", "Access grant suspended"),
                    ("ACCESS_GRANT_REVOKED", "Access grant revoked"),
                    ("IAS_BOOTSTRAPPED", "IAS identity bootstrapped"),
                    ("ACTIVATION_CONSUMED", "Activation consumed"),
                    ("APPROVAL_CASE_CREATED", "Approval case created"),
                    ("APPROVAL_CASE_APPROVED", "Approval case approved"),
                    ("APPROVAL_CASE_REJECTED", "Approval case rejected"),
                    ("ACCESS_REQUEST_STATE_IMPORTED", "Access request history imported"),
                    ("DOCUMENT_SUBMITTED", "Document submitted"),
                    ("DOCUMENT_VALIDATION_FAILED", "Document validation failed"),
                    ("DOCUMENT_READY_FOR_APPROVAL", "Document ready for approval"),
                    ("DOCUMENT_APPROVED", "Document approved"),
                    ("DOCUMENT_REJECTED", "Document rejected"),
                    ("DOCUMENT_INGESTION_QUEUED", "Document ingestion queued"),
                    ("DOCUMENT_INGESTION_RETRIED", "Document ingestion retried"),
                    ("DOCUMENT_INGESTION_SUCCEEDED", "Document ingestion succeeded"),
                    ("DOCUMENT_INGESTION_FAILED", "Document ingestion failed"),
                ],
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="governanceauditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(category__in=[
                    "IDENTITY_ACCESS", "APPROVAL", "AUTHORIZATION", "SECURITY", "SYSTEM", "DOCUMENT",
                ]),
                name="gov_audit_category_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="governanceauditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(action__in=DOCUMENT_ACTIONS),
                name="gov_audit_action_valid",
            ),
        ),
    ]
