from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

import psycopg
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.identity_access.models import (
    AccessGrant,
    AccessGrantState,
    AccessRequest,
    AccessRequestState,
    AccessRequestTransition,
    AccessRole,
    ActivationToken,
    IASAuthorityScope,
    OrganizationUnit,
    ValidationOutcome,
)
from apps.identity_access.services import (
    AccessDecisionError,
    _get_locked_access_request,
    approve_access_request,
    reject_access_request,
    valid_activation_token,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ACCESS_ALLOWED_EMAIL_DOMAINS=(),
    ACCESS_ALLOWED_ORGANIZATION_CODES=(),
    ACCESS_REQUEST_SOURCE_LIMIT=100,
    ACCESS_REQUEST_EMAIL_LIMIT=100,
    ACCESS_REQUEST_IDENTITY_LIMIT=100,
    ACCESS_LOGIN_SOURCE_LIMIT=100,
    ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT=100,
)
class PostgreSQLAccessWorkflowTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise AssertionError("This integration suite must run against PostgreSQL.")

    def setUp(self):
        User = get_user_model()
        self.org = OrganizationUnit.objects.create(code="pg-environment", name="PG Environment")
        self.other_org = OrganizationUnit.objects.create(code="pg-finance", name="PG Finance")
        self.ias = User.objects.create_user("pg-ias", "pg-ias@example.gov", "TestOnlyPass-9321")
        AccessGrant.objects.create(user=self.ias, organization=self.org, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.org)

    def make_request(self, suffix: str, *, organization=None, requester=None, email=None):
        email = email or f"pg-{suffix}@example.gov"
        access_request = AccessRequest.objects.create(
            full_name=f"PostgreSQL Officer {suffix}",
            official_email=email,
            normalized_email=email.casefold(),
            employee_identifier=f"PG-{suffix}",
            normalized_identity=f"pg{suffix}".casefold(),
            organization=organization or self.org,
            designation="Officer",
            reason="PostgreSQL workflow validation",
            state=AccessRequestState.PENDING_IAS_APPROVAL,
            validation_outcome=ValidationOutcome.ALLOW_TO_QUEUE,
            source_fingerprint="c" * 64,
            status_token_digest="d" * 64,
            requester_user=requester,
            version=3,
            expires_at=timezone.now() + timedelta(days=2),
        )
        transitions = (
            (1, "", AccessRequestState.REQUESTED),
            (2, AccessRequestState.REQUESTED, AccessRequestState.PENDING_VALIDATION),
            (3, AccessRequestState.PENDING_VALIDATION, AccessRequestState.PENDING_IAS_APPROVAL),
        )
        AccessRequestTransition.objects.bulk_create([
            AccessRequestTransition(
                request=access_request,
                old_state=old_state,
                new_state=new_state,
                reason="PostgreSQL fixture",
                version=version,
            )
            for version, old_state, new_state in transitions
        ])
        return access_request

    def lock_sql(self, queries):
        return [
            item["sql"] for item in queries
            if "FOR UPDATE" in item["sql"] and "identity_access_accessrequest" in item["sql"]
        ]

    def test_nullable_requester_approval_uses_base_row_lock_and_creates_activation(self):
        access_request = self.make_request("approve-null")
        with CaptureQueriesContext(connection) as captured:
            approved = approve_access_request(self.ias, access_request.pk, expected_version=3, reason="verified")
        lock_sql = self.lock_sql(captured.captured_queries)
        self.assertTrue(lock_sql)
        self.assertTrue(all('FOR UPDATE OF "identity_access_accessrequest"' in sql for sql in lock_sql))
        self.assertTrue(all("LEFT OUTER JOIN" not in sql for sql in lock_sql))
        approved.request.refresh_from_db()
        self.assertEqual(approved.request.state, AccessRequestState.APPROVED)
        self.assertEqual(approved.request.version, 4)
        self.assertTrue(approved.request.transitions.filter(version=4, new_state=AccessRequestState.APPROVED).exists())
        grant = AccessGrant.objects.get(user_id=approved.user_id, organization=self.org, role=AccessRole.OFFICER)
        self.assertEqual(grant.state, AccessGrantState.ACTIVE)
        self.assertFalse(AccessGrant.objects.filter(user_id=approved.user_id, role=AccessRole.IAS_OFFICER).exists())
        self.assertIsNotNone(valid_activation_token(approved.user_id, approved.activation_token))

    def test_non_null_other_requester_can_be_approved(self):
        requester = get_user_model().objects.create_user("pg-requester", "pg-requester@example.gov", "TestOnlyPass-9321")
        access_request = self.make_request("approve-owned", requester=requester, email=requester.email)
        approved = approve_access_request(self.ias, access_request.pk, expected_version=3, reason="different actor")
        self.assertEqual(approved.user_id, requester.pk)
        self.assertTrue(AccessGrant.objects.filter(user=requester, role=AccessRole.OFFICER).exists())

    def test_genuine_self_decision_is_denied_for_approval_and_rejection(self):
        access_request = self.make_request("self", requester=self.ias, email=self.ias.email)
        with self.assertRaisesMessage(AccessDecisionError, "Self-approval"):
            approve_access_request(self.ias, access_request.pk, expected_version=3, reason="self")
        with self.assertRaisesMessage(AccessDecisionError, "Self-decision"):
            reject_access_request(self.ias, access_request.pk, expected_version=3, reason="self")
        access_request.refresh_from_db()
        self.assertEqual(access_request.state, AccessRequestState.PENDING_IAS_APPROVAL)
        self.assertEqual(access_request.version, 3)

    def test_approval_repeat_stale_and_wrong_scope_fail_safely(self):
        approved_request = self.make_request("approve-repeat")
        approve_access_request(self.ias, approved_request.pk, expected_version=3, reason="verified")
        with self.assertRaisesMessage(AccessDecisionError, "Only pending"):
            approve_access_request(self.ias, approved_request.pk, expected_version=4, reason="repeat")
        stale = self.make_request("approve-stale")
        with self.assertRaisesMessage(AccessDecisionError, "changed"):
            approve_access_request(self.ias, stale.pk, expected_version=2, reason="stale")
        outside = self.make_request("approve-scope", organization=self.other_org)
        with self.assertRaisesMessage(AccessDecisionError, "outside your organizational scope"):
            approve_access_request(self.ias, outside.pk, expected_version=3, reason="outside")

    def test_nullable_requester_rejection_uses_base_row_lock_without_side_effects(self):
        access_request = self.make_request("reject-null")
        with CaptureQueriesContext(connection) as captured:
            rejected = reject_access_request(self.ias, access_request.pk, expected_version=3, reason="identity not verified")
        lock_sql = self.lock_sql(captured.captured_queries)
        self.assertTrue(lock_sql)
        self.assertTrue(all('FOR UPDATE OF "identity_access_accessrequest"' in sql for sql in lock_sql))
        self.assertTrue(all("LEFT OUTER JOIN" not in sql for sql in lock_sql))
        self.assertEqual(rejected.state, AccessRequestState.REJECTED)
        self.assertEqual(rejected.version, 4)
        self.assertEqual(rejected.decision_reason, "identity not verified")
        self.assertTrue(rejected.transitions.filter(version=4, new_state=AccessRequestState.REJECTED).exists())
        self.assertFalse(AccessGrant.objects.filter(user__email=access_request.normalized_email).exists())
        self.assertFalse(ActivationToken.objects.filter(access_request=access_request).exists())

    def test_non_null_other_requester_can_be_rejected_without_grant_or_token(self):
        requester = get_user_model().objects.create_user("pg-reject-requester", "pg-reject-requester@example.gov", "TestOnlyPass-9321")
        access_request = self.make_request("reject-owned", requester=requester, email=requester.email)
        rejected = reject_access_request(self.ias, access_request.pk, expected_version=3, reason="not verified")
        self.assertEqual(rejected.state, AccessRequestState.REJECTED)
        self.assertFalse(AccessGrant.objects.filter(user=requester, role=AccessRole.OFFICER).exists())
        self.assertFalse(ActivationToken.objects.filter(access_request=access_request).exists())

    def test_rejection_repeat_stale_and_wrong_scope_fail_safely(self):
        rejected_request = self.make_request("reject-repeat")
        reject_access_request(self.ias, rejected_request.pk, expected_version=3, reason="not verified")
        with self.assertRaisesMessage(AccessDecisionError, "Only pending"):
            reject_access_request(self.ias, rejected_request.pk, expected_version=4, reason="repeat")
        stale = self.make_request("reject-stale")
        with self.assertRaisesMessage(AccessDecisionError, "changed"):
            reject_access_request(self.ias, stale.pk, expected_version=2, reason="stale")
        outside = self.make_request("reject-scope", organization=self.other_org)
        with self.assertRaisesMessage(AccessDecisionError, "outside your organizational scope"):
            reject_access_request(self.ias, outside.pk, expected_version=3, reason="outside")

    def test_base_request_row_is_really_locked_against_second_postgresql_connection(self):
        access_request = self.make_request("lock")
        database = connection.settings_dict
        with transaction.atomic():
            _get_locked_access_request(access_request.pk)
            other = psycopg.connect(
                dbname=database["NAME"],
                user=database["USER"],
                password=database["PASSWORD"],
                host=database["HOST"],
                port=database["PORT"],
            )
            try:
                with other.cursor() as cursor:
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        cursor.execute(
                            "SELECT id FROM identity_access_accessrequest WHERE id = %s FOR UPDATE NOWAIT",
                            (access_request.pk,),
                        )
                other.rollback()
            finally:
                other.close()

    def test_full_http_approval_activation_login_and_rejection_workflow(self):
        def csrf(client: Client, url: str) -> str:
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            return client.cookies["csrftoken"].value

        request_url = reverse("identity_access:request_access")
        login_url = reverse("identity_access:login")

        anonymous = Client(enforce_csrf_checks=True)
        response = anonymous.post(
            request_url,
            {
                "csrfmiddlewaretoken": csrf(anonymous, request_url),
                "full_name": "PostgreSQL HTTP Approval Officer",
                "official_email": "pg-http-approve@example.gov",
                "employee_identifier": "PG-HTTP-APPROVE",
                "organization": self.org.pk,
                "designation": "Officer",
                "reason": "PostgreSQL HTTP workflow validation",
            },
            REMOTE_ADDR="198.51.100.210",
        )
        self.assertEqual(response.status_code, 200)
        approval_request = AccessRequest.objects.get(normalized_email="pg-http-approve@example.gov")
        self.assertIsNone(approval_request.requester_user_id)
        self.assertEqual(approval_request.state, AccessRequestState.PENDING_IAS_APPROVAL)

        ias_client = Client(enforce_csrf_checks=True)
        response = ias_client.post(
            login_url,
            {
                "csrfmiddlewaretoken": csrf(ias_client, login_url),
                "identifier": self.ias.username,
                "password": "TestOnlyPass-9321",
                "next": "",
            },
            REMOTE_ADDR="198.51.100.211",
        )
        self.assertEqual(response.status_code, 302)
        detail_url = reverse("identity_access:ias_request_detail", args=[approval_request.pk])
        self.assertEqual(ias_client.get(detail_url).status_code, 200)
        response = ias_client.post(
            reverse("identity_access:ias_approve_request", args=[approval_request.pk]),
            {
                "csrfmiddlewaretoken": ias_client.cookies["csrftoken"].value,
                "expected_version": approval_request.version,
                "decision_reason": "Identity verified by PostgreSQL HTTP integration",
            },
        )
        self.assertEqual(response.status_code, 200)
        activation_path = urlsplit(response.context["activation_url"]).path
        approval_request.refresh_from_db()
        self.assertEqual(approval_request.state, AccessRequestState.APPROVED)
        self.assertEqual(approval_request.version, 4)

        officer = get_user_model().objects.get(email="pg-http-approve@example.gov")
        self.assertTrue(AccessGrant.objects.filter(user=officer, role=AccessRole.OFFICER).exists())
        token_record = ActivationToken.objects.get(access_request=approval_request)
        self.assertIsNone(token_record.used_at)

        officer_client = Client(enforce_csrf_checks=True)
        response = officer_client.post(
            activation_path,
            {
                "csrfmiddlewaretoken": csrf(officer_client, activation_path),
                "new_password1": "ActivatedPass-9321",
                "new_password2": "ActivatedPass-9321",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("search_ui:search"))
        self.assertEqual(officer_client.get(reverse("search_ui:search")).status_code, 200)
        token_record.refresh_from_db()
        self.assertIsNotNone(token_record.used_at)
        response = officer_client.post(
            reverse("identity_access:logout"),
            {"csrfmiddlewaretoken": officer_client.cookies["csrftoken"].value},
        )
        self.assertEqual(response.status_code, 302)
        response = officer_client.post(
            login_url,
            {
                "csrfmiddlewaretoken": csrf(officer_client, login_url),
                "identifier": officer.username,
                "password": "ActivatedPass-9321",
                "next": "",
            },
            REMOTE_ADDR="198.51.100.213",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(officer_client.get(reverse("search_ui:search")).status_code, 200)

        rejected_anonymous = Client(enforce_csrf_checks=True)
        response = rejected_anonymous.post(
            request_url,
            {
                "csrfmiddlewaretoken": csrf(rejected_anonymous, request_url),
                "full_name": "PostgreSQL HTTP Rejection Officer",
                "official_email": "pg-http-reject@example.gov",
                "employee_identifier": "PG-HTTP-REJECT",
                "organization": self.org.pk,
                "designation": "Officer",
                "reason": "PostgreSQL HTTP rejection validation",
            },
            REMOTE_ADDR="198.51.100.212",
        )
        self.assertEqual(response.status_code, 200)
        rejection_request = AccessRequest.objects.get(normalized_email="pg-http-reject@example.gov")
        self.assertIsNone(rejection_request.requester_user_id)
        rejection_detail_url = reverse("identity_access:ias_request_detail", args=[rejection_request.pk])
        self.assertEqual(ias_client.get(rejection_detail_url).status_code, 200)
        response = ias_client.post(
            reverse("identity_access:ias_reject_request", args=[rejection_request.pk]),
            {
                "csrfmiddlewaretoken": ias_client.cookies["csrftoken"].value,
                "expected_version": rejection_request.version,
                "decision_reason": "Identity not verified in PostgreSQL HTTP integration",
            },
        )
        self.assertEqual(response.status_code, 302)
        rejection_request.refresh_from_db()
        self.assertEqual(rejection_request.state, AccessRequestState.REJECTED)
        self.assertEqual(rejection_request.version, 4)
        self.assertFalse(AccessGrant.objects.filter(user__email=rejection_request.normalized_email).exists())
        self.assertFalse(ActivationToken.objects.filter(access_request=rejection_request).exists())
