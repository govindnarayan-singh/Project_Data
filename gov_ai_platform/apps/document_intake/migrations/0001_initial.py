import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


def widen_corpus_origin(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        DO $$
        DECLARE constraint_name text;
        BEGIN
            IF to_regclass('public.documents') IS NULL THEN
                RETURN;
            END IF;
            FOR constraint_name IN
                SELECT c.conname
                  FROM pg_constraint c
                 WHERE c.conrelid = 'public.documents'::regclass
                   AND c.contype = 'c'
                   AND pg_get_constraintdef(c.oid) LIKE '%%source_origin%%'
            LOOP
                EXECUTE format('ALTER TABLE public.documents DROP CONSTRAINT %%I', constraint_name);
            END LOOP;
            ALTER TABLE public.documents
                ADD CONSTRAINT ck_documents_source_origin
                CHECK (source_origin IN ('LOCAL_ORIGINAL','EXTERNAL_OFFICIAL','APPROVED_SUBMISSION'));
        END $$;
    """)


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("governance", "0002_stage3_document_audit"),
        ("identity_access", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="DocumentSubmission",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("submitter_role_snapshot", models.CharField(max_length=32)),
                ("original_filename", models.CharField(max_length=255)),
                ("storage_key", models.CharField(max_length=255, unique=True)),
                ("sha256", models.CharField(db_index=True, max_length=64)),
                ("byte_size", models.PositiveBigIntegerField()),
                ("declared_mime", models.CharField(max_length=128)),
                ("detected_mime", models.CharField(default="application/pdf", max_length=128)),
                ("state", models.CharField(choices=[
                    ("VALIDATION_FAILED", "Validation failed"), ("PENDING_IAS_APPROVAL", "Pending IAS approval"),
                    ("REJECTED", "Rejected"), ("QUEUED_FOR_INGESTION", "Approved — queued for ingestion"),
                    ("INGESTING", "Approved — ingesting"), ("INGESTED", "Ingested"),
                    ("INGESTION_FAILED", "Approved — ingestion failed"),
                ], max_length=32)),
                ("validation_state", models.CharField(choices=[("PASSED", "Passed"), ("FAILED", "Failed")], max_length=16)),
                ("security_status", models.CharField(choices=[("PASSED", "Structural validation passed"), ("FAILED", "Security validation failed")], max_length=16)),
                ("malware_status", models.CharField(choices=[
                    ("CLEAN", "Scanner reported clean"), ("MALWARE_SCAN_NOT_CONFIGURED", "Malware scanner not configured"),
                    ("MALICIOUS", "Malicious content detected"), ("SCAN_ERROR", "Malware scan failed"),
                ], max_length=40)),
                ("source_provenance", models.CharField(choices=[
                    ("AUTHORITATIVE_SOURCE_VERIFIED", "Authoritative source independently verified"),
                    ("IAS_VERIFIED_COPY", "IAS verified copy"),
                    ("OFFICER_PROVIDED_UNVERIFIED", "Officer-provided, unverified"), ("UNKNOWN", "Unknown"),
                ], max_length=48)),
                ("claimed_source_url", models.URLField(blank=True, max_length=1000)),
                ("claimed_identifier", models.CharField(blank=True, max_length=255)),
                ("submission_reason", models.CharField(max_length=1000)),
                ("detected_metadata", models.JSONField(blank=True, default=dict)),
                ("duplicate_status", models.CharField(choices=[
                    ("NEW", "New"), ("EXACT_PENDING_DUPLICATE", "Exact pending duplicate"),
                    ("EXACT_CORPUS_DUPLICATE", "Exact corpus duplicate"),
                    ("POSSIBLE_IDENTITY_DUPLICATE", "Possible identity duplicate"),
                ], default="NEW", max_length=40)),
                ("matched_document_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("archive_key", models.CharField(blank=True, max_length=255)),
                ("ingested_document_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("page_count", models.PositiveIntegerField(default=0)),
                ("extraction_status", models.CharField(blank=True, max_length=40)),
                ("preview_text", models.TextField(blank=True)),
                ("validation_findings", models.JSONField(blank=True, default=list)),
                ("risk_flags", models.JSONField(blank=True, default=list)),
                ("origin", models.CharField(choices=[
                    ("DIRECT_UPLOAD", "Direct upload"), ("UNRESOLVED_REFERENCE", "Unresolved reference"),
                    ("QUERY_MISSING_REFERENCE", "Query missing reference"),
                ], default="DIRECT_UPLOAD", max_length=32)),
                ("origin_reference", models.CharField(blank=True, max_length=128)),
                ("correlation_id", models.UUIDField(db_index=True, default=uuid.uuid4)),
                ("safe_failure_code", models.CharField(blank=True, max_length=64)),
                ("safe_failure_reason", models.CharField(blank=True, max_length=500)),
                ("internal_failure_detail", models.TextField(blank=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approval_case", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="document_submission", to="governance.governanceapprovalcase")),
                ("matched_submission", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="duplicate_submissions", to="document_intake.documentsubmission")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="document_submissions", to="identity_access.organizationunit")),
                ("submitted_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="document_submissions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="DocumentIngestionJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("state", models.CharField(choices=[
                    ("PENDING", "Pending"), ("RUNNING", "Running"), ("SUCCEEDED", "Succeeded"),
                    ("RETRYABLE", "Retryable"), ("FAILED", "Failed"),
                ], default="PENDING", max_length=16)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveIntegerField(default=3)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("worker_id", models.CharField(blank=True, max_length=128)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("ingestion_run_id", models.CharField(blank=True, max_length=64)),
                ("last_error_code", models.CharField(blank=True, max_length=64)),
                ("safe_error_reason", models.CharField(blank=True, max_length=500)),
                ("internal_error_detail", models.TextField(blank=True)),
                ("kg_result", models.JSONField(blank=True, default=dict)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("submission", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="ingestion_job", to="document_intake.documentsubmission")),
            ],
            options={"ordering": ("created_at",)},
        ),
        migrations.AddIndex(model_name="documentsubmission", index=models.Index(fields=["organization", "state", "created_at"], name="doc_scope_state_idx")),
        migrations.AddIndex(model_name="documentsubmission", index=models.Index(fields=["submitted_by", "created_at"], name="doc_submitter_time_idx")),
        migrations.AddConstraint(model_name="documentsubmission", constraint=models.CheckConstraint(condition=models.Q(state__in=[
            "VALIDATION_FAILED", "PENDING_IAS_APPROVAL", "REJECTED", "QUEUED_FOR_INGESTION",
            "INGESTING", "INGESTED", "INGESTION_FAILED",
        ]), name="doc_submission_state_valid")),
        migrations.AddConstraint(model_name="documentsubmission", constraint=models.CheckConstraint(condition=models.Q(validation_state__in=["PASSED", "FAILED"]), name="doc_validation_state_valid")),
        migrations.AddConstraint(model_name="documentsubmission", constraint=models.CheckConstraint(condition=models.Q(security_status__in=["PASSED", "FAILED"]), name="doc_security_state_valid")),
        migrations.AddConstraint(model_name="documentsubmission", constraint=models.CheckConstraint(condition=models.Q(duplicate_status__in=[
            "NEW", "EXACT_PENDING_DUPLICATE", "EXACT_CORPUS_DUPLICATE", "POSSIBLE_IDENTITY_DUPLICATE",
        ]), name="doc_duplicate_state_valid")),
        migrations.AddConstraint(model_name="documentsubmission", constraint=models.UniqueConstraint(
            condition=models.Q(state__in=("PENDING_IAS_APPROVAL", "QUEUED_FOR_INGESTION", "INGESTING", "INGESTED")),
            fields=("sha256",), name="doc_one_active_sha256",
        )),
        migrations.AddIndex(model_name="documentingestionjob", index=models.Index(fields=["state", "created_at"], name="doc_job_claim_idx")),
        migrations.AddConstraint(model_name="documentingestionjob", constraint=models.CheckConstraint(condition=models.Q(state__in=[
            "PENDING", "RUNNING", "SUCCEEDED", "RETRYABLE", "FAILED",
        ]), name="doc_job_state_valid")),
        migrations.RunPython(widen_corpus_origin, migrations.RunPython.noop),
    ]
