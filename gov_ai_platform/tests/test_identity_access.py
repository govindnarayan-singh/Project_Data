from __future__ import annotations

import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.identity_access.forms import AccessRequestForm
from apps.identity_access.models import (
    AccessGrant,
    AccessGrantState,
    AccessRequest,
    AccessRequestState,
    AccessRole,
    ActivationToken,
    IASAuthorityScope,
    OrganizationUnit,
    ValidationOutcome,
)
from apps.identity_access.services import (
    AccessDecisionError,
    AccessWorkflowError,
    activate_account,
    approve_access_request,
    change_officer_grant_state,
    client_source,
    create_activation_token,
    expire_stale_requests,
    reject_access_request,
    submit_access_request,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ACCESS_ALLOWED_EMAIL_DOMAINS=(),
    ACCESS_ALLOWED_ORGANIZATION_CODES=(),
    ACCESS_ALIAS_NORMALIZATION_DOMAINS=("example.gov",),
    ACCESS_REQUEST_SOURCE_LIMIT=100,
    ACCESS_REQUEST_EMAIL_LIMIT=100,
    ACCESS_REQUEST_IDENTITY_LIMIT=100,
    ACCESS_LOGIN_SOURCE_LIMIT=100,
    ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT=100,
)
class IdentityAccessSecurityTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.factory = RequestFactory()
        self.org_a = OrganizationUnit.objects.create(code="environment", name="Environment")
        self.org_b = OrganizationUnit.objects.create(code="finance", name="Finance")
        self.ias = self.User.objects.create_user("ias-a", "ias-a@example.gov", "SafePass-9321")
        AccessGrant.objects.create(user=self.ias, organization=self.org_a, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.org_a)
        self.officer = self.User.objects.create_user("officer-a", "officer-a@example.gov", "SafePass-9321")
        self.officer_grant = AccessGrant.objects.create(user=self.officer, organization=self.org_a, role=AccessRole.OFFICER)

    def make_access_request(self, *, org=None, email="new@example.gov", employee_id="EMP-100", requester=None, state=AccessRequestState.PENDING_IAS_APPROVAL):
        return AccessRequest.objects.create(
            full_name="New Officer",
            official_email=email,
            normalized_email=email.casefold(),
            employee_identifier=employee_id,
            normalized_identity=employee_id.casefold().replace("-", ""),
            organization=org or self.org_a,
            designation="Officer",
            reason="Official duties",
            state=state,
            validation_outcome=ValidationOutcome.ALLOW_TO_QUEUE,
            source_fingerprint="a" * 64,
            status_token_digest="b" * 64,
            requester_user=requester,
            version=3,
            expires_at=timezone.now() + timedelta(days=2),
            decided_at=timezone.now() if state == AccessRequestState.REJECTED else None,
        )

    def submission(self, *, email="new@example.gov", employee_id="EMP-100", org=None, remote="198.51.100.10", extra=None):
        request = self.factory.post("/access/request/", REMOTE_ADDR=remote)
        request.user = AnonymousUser()
        cleaned = {
            "full_name": "New Officer",
            "official_email": email,
            "employee_identifier": employee_id,
            "organization": org or self.org_a,
            "designation": "Officer",
            "reason": "Official duties",
        }
        cleaned.update(extra or {})
        return submit_access_request(cleaned, request)

    def test_anonymous_and_unapproved_identities_cannot_access_protected_app(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("identity_access:login"), response["Location"])

        pending_user = self.User.objects.create_user("pending", "pending@example.gov", "SafePass-9321")
        self.client.force_login(pending_user)
        self.assertRedirects(self.client.get("/"), reverse("identity_access:access_denied"), fetch_redirect_response=False)
        self.client.logout()

        rejected_user = self.User.objects.create_user("rejected", "rejected@example.gov", "SafePass-9321")
        self.make_access_request(email="rejected@example.gov", employee_id="REJ-1", requester=rejected_user, state=AccessRequestState.REJECTED)
        self.client.force_login(rejected_user)
        self.assertRedirects(self.client.get("/"), reverse("identity_access:access_denied"), fetch_redirect_response=False)

    def test_active_officer_allowed_and_expired_suspended_revoked_denied(self):
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get("/").status_code, 200)
        for state in (AccessGrantState.SUSPENDED, AccessGrantState.REVOKED):
            self.officer_grant.state = state
            self.officer_grant.save(update_fields=("state",))
            self.assertRedirects(self.client.get("/"), reverse("identity_access:access_denied"), fetch_redirect_response=False)
            self.officer_grant.state = AccessGrantState.ACTIVE
            self.officer_grant.save(update_fields=("state",))
        self.officer_grant.starts_at = timezone.now() - timedelta(days=2)
        self.officer_grant.expires_at = timezone.now() - timedelta(days=1)
        self.officer_grant.save(update_fields=("starts_at", "expires_at"))
        self.assertRedirects(self.client.get("/"), reverse("identity_access:access_denied"), fetch_redirect_response=False)

    def test_officer_cannot_view_or_act_as_ias(self):
        access_request = self.make_access_request()
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get(reverse("identity_access:ias_queue")).status_code, 403)
        self.assertEqual(self.client.post(reverse("identity_access:ias_approve_request", args=[access_request.pk]), {"expected_version": 3, "decision_reason": "forged"}).status_code, 403)
        self.officer_grant.refresh_from_db()
        self.assertEqual(self.officer_grant.role, AccessRole.OFFICER)

    def test_self_approval_and_out_of_scope_approval_are_blocked(self):
        self_request = self.make_access_request(requester=self.ias)
        with self.assertRaisesMessage(AccessDecisionError, "Self-approval"):
            approve_access_request(self.ias, self_request.pk, expected_version=3, reason="no")
        other = self.make_access_request(org=self.org_b, email="finance@example.gov", employee_id="FIN-1")
        with self.assertRaisesMessage(AccessDecisionError, "outside your organizational scope"):
            approve_access_request(self.ias, other.pk, expected_version=3, reason="no")

    def test_ias_queue_and_detail_are_object_scoped(self):
        allowed = self.make_access_request()
        denied = self.make_access_request(org=self.org_b, email="finance@example.gov", employee_id="FIN-1")
        self.client.force_login(self.ias)
        response = self.client.get(reverse("identity_access:ias_queue"))
        self.assertContains(response, str(allowed.official_email))
        self.assertNotContains(response, str(denied.official_email))
        self.assertEqual(self.client.get(reverse("identity_access:ias_request_detail", args=[denied.pk])).status_code, 404)

    def test_duplicate_pending_and_alias_variation_are_blocked(self):
        self.submission(email="person+first@example.gov", employee_id="EMP-201")
        with self.assertRaises(AccessWorkflowError) as caught:
            self.submission(email="PERSON+second@example.gov", employee_id="EMP-202")
        self.assertEqual(caught.exception.code, ValidationOutcome.REJECT_DUPLICATE)
        with self.assertRaises(AccessWorkflowError) as caught_identity:
            self.submission(email="other@example.gov", employee_id="EMP 201")
        self.assertEqual(caught_identity.exception.code, ValidationOutcome.REJECT_DUPLICATE)

    @override_settings(ACCESS_REQUEST_SOURCE_LIMIT=2)
    def test_request_flood_is_source_throttled(self):
        self.submission(email="one@example.gov", employee_id="ONE")
        self.submission(email="two@example.gov", employee_id="TWO")
        with self.assertRaises(AccessWorkflowError) as caught:
            self.submission(email="three@example.gov", employee_id="THREE")
        self.assertEqual(caught.exception.code, ValidationOutcome.RATE_LIMITED)

    def test_rejection_cooldown_is_enforced(self):
        self.make_access_request(email="cool@example.gov", employee_id="COOL-1", state=AccessRequestState.REJECTED)
        with self.assertRaises(AccessWorkflowError) as caught:
            self.submission(email="cool@example.gov", employee_id="COOL-2")
        self.assertEqual(caught.exception.code, ValidationOutcome.COOLDOWN)

    def test_stale_approval_rejected_and_approval_is_transactional(self):
        access_request = self.make_access_request()
        with self.assertRaisesMessage(AccessDecisionError, "changed"):
            approve_access_request(self.ias, access_request.pk, expected_version=2, reason="stale")
        access_request.refresh_from_db()
        self.assertEqual(access_request.state, AccessRequestState.PENDING_IAS_APPROVAL)
        approved = approve_access_request(self.ias, access_request.pk, expected_version=3, reason="verified")
        approved.request.refresh_from_db()
        self.assertEqual(approved.request.state, AccessRequestState.APPROVED)
        self.assertTrue(AccessGrant.objects.filter(user_id=approved.user_id, role=AccessRole.OFFICER, state=AccessGrantState.ACTIVE).exists())

    def test_decision_endpoints_are_post_only_and_csrf_protected(self):
        access_request = self.make_access_request()
        self.client.force_login(self.ias)
        self.assertEqual(self.client.get(reverse("identity_access:ias_approve_request", args=[access_request.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("identity_access:ias_reject_request", args=[access_request.pk])).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.ias)
        response = csrf_client.post(reverse("identity_access:ias_approve_request", args=[access_request.pk]), {"expected_version": 3, "decision_reason": "valid"})
        self.assertEqual(response.status_code, 403)

    def test_public_role_and_privilege_parameter_tampering_is_rejected(self):
        form = AccessRequestForm(data={
            "full_name": "Attacker",
            "official_email": "attack@example.gov",
            "employee_identifier": "ATK-1",
            "organization": self.org_a.pk,
            "reason": "test",
            "role": "IAS_OFFICER",
            "is_superuser": "1",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("Unsupported privilege", str(form.non_field_errors()))
        self.assertFalse(self.User.objects.filter(email="attack@example.gov").exists())

    def test_public_submission_creates_request_but_no_active_account(self):
        submitted = self.submission()
        self.assertEqual(submitted.request.state, AccessRequestState.PENDING_IAS_APPROVAL)
        self.assertEqual(submitted.request.transitions.count(), 3)
        self.assertFalse(self.User.objects.filter(email="new@example.gov").exists())

    def test_activation_token_is_single_use_hashed_and_expiring(self):
        user = self.User.objects.create_user("activate", "activate@example.gov")
        AccessGrant.objects.create(user=user, organization=self.org_a, role=AccessRole.OFFICER)
        raw = create_activation_token(user, actor=self.ias)
        token_record = ActivationToken.objects.get(user=user)
        self.assertNotEqual(token_record.token_digest, raw)
        activated = activate_account(user.pk, raw, "NewSafePass-4521")
        self.assertTrue(activated.check_password("NewSafePass-4521"))
        with self.assertRaises(AccessWorkflowError):
            activate_account(user.pk, raw, "AnotherPass-4521")
        expired = create_activation_token(user, actor=self.ias)
        ActivationToken.objects.filter(user=user, used_at__isnull=True).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(AccessWorkflowError):
            activate_account(user.pk, expired, "AnotherPass-4521")

    def test_login_error_is_generic_for_unknown_and_known_accounts(self):
        login_url = reverse("identity_access:login")
        known = self.client.post(login_url, {"identifier": self.officer.username, "password": "wrong"})
        unknown = self.client.post(login_url, {"identifier": "does-not-exist", "password": "wrong"}, REMOTE_ADDR="198.51.100.22")
        generic = "Unable to sign in with the supplied credentials."
        self.assertContains(known, generic)
        self.assertContains(unknown, generic)
        self.assertNotContains(known, "exists")
        self.assertNotContains(unknown, "unknown")

    @override_settings(ACCESS_LOGIN_SOURCE_LIMIT=1, ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT=1)
    def test_login_brute_force_limit_is_temporary_and_generic(self):
        login_url = reverse("identity_access:login")
        first = self.client.post(login_url, {"identifier": self.officer.username, "password": "wrong"}, REMOTE_ADDR="198.51.100.90")
        blocked = self.client.post(login_url, {"identifier": self.officer.username, "password": "SafePass-9321"}, REMOTE_ADDR="198.51.100.90")
        self.assertContains(first, "Unable to sign in with the supplied credentials.")
        self.assertContains(blocked, "Unable to sign in with the supplied credentials.")
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(ACCESS_TRUSTED_PROXY_NETWORKS=())
    def test_untrusted_forwarded_for_is_ignored(self):
        request = self.factory.get("/", REMOTE_ADDR="198.51.100.8", HTTP_X_FORWARDED_FOR="203.0.113.4")
        self.assertEqual(client_source(request), "198.51.100.8")

    @override_settings(ACCESS_TRUSTED_PROXY_NETWORKS=("198.51.100.0/24",))
    def test_forwarded_for_requires_explicit_trusted_proxy(self):
        request = self.factory.get("/", REMOTE_ADDR="198.51.100.8", HTTP_X_FORWARDED_FOR="203.0.113.4")
        self.assertEqual(client_source(request), "203.0.113.4")

    def test_revocation_removes_access_on_next_request(self):
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get("/").status_code, 200)
        change_officer_grant_state(self.ias, self.officer_grant.pk, expected_version=1, new_state=AccessGrantState.REVOKED, reason="security response")
        self.assertRedirects(self.client.get("/"), reverse("identity_access:access_denied"), fetch_redirect_response=False)

    def test_login_rotates_session_and_rejects_open_redirect(self):
        client = Client()
        session = client.session
        session["preauth"] = "marker"
        session.save()
        old_key = session.session_key
        response = client.post(reverse("identity_access:login"), {
            "identifier": self.officer.username,
            "password": "SafePass-9321",
            "next": "https://evil.example/steal",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("search_ui:search"))
        self.assertNotEqual(client.session.session_key, old_key)

    def test_reject_transition_and_scope_are_versioned(self):
        access_request = self.make_access_request()
        rejected = reject_access_request(self.ias, access_request.pk, expected_version=3, reason="identity not verified")
        self.assertEqual(rejected.state, AccessRequestState.REJECTED)
        self.assertEqual(rejected.version, 4)
        with self.assertRaises(AccessDecisionError):
            reject_access_request(self.ias, access_request.pk, expected_version=3, reason="again")

    def test_request_expiry_releases_pending_identity(self):
        access_request = self.make_access_request()
        AccessRequest.objects.filter(pk=access_request.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_stale_requests(), 1)
        access_request.refresh_from_db()
        self.assertEqual(access_request.state, AccessRequestState.EXPIRED)
        replacement = self.submission(email=access_request.normalized_email, employee_id=access_request.employee_identifier)
        self.assertEqual(replacement.request.state, AccessRequestState.PENDING_IAS_APPROVAL)

    def test_expired_request_cannot_be_approved_and_transition_is_committed(self):
        access_request = self.make_access_request(email="expired@example.gov", employee_id="EXPIRED-1")
        AccessRequest.objects.filter(pk=access_request.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(AccessDecisionError, "expired"):
            approve_access_request(self.ias, access_request.pk, expected_version=3, reason="too late")
        access_request.refresh_from_db()
        self.assertEqual(access_request.state, AccessRequestState.EXPIRED)
        self.assertEqual(access_request.version, 4)

    def test_database_constraint_blocks_concurrent_duplicate_pending_email(self):
        self.make_access_request()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_access_request(email="new@example.gov", employee_id="EMP-OTHER")

    def test_ias_queue_is_server_paginated(self):
        for index in range(26):
            self.make_access_request(email=f"queue-{index}@example.gov", employee_id=f"QUEUE-{index}")
        self.client.force_login(self.ias)
        response = self.client.get(reverse("identity_access:ias_queue"))
        self.assertEqual(len(response.context["page_obj"].object_list), 25)
        self.assertEqual(response.context["page_obj"].paginator.count, 26)

    def test_security_headers_secure_cookie_attributes_and_logout_flush(self):
        login_url = reverse("identity_access:login")
        response = self.client.get(login_url)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.client.post(login_url, {"identifier": self.officer.username, "password": "SafePass-9321"})
        cookie = self.client.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.client.post(reverse("identity_access:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_status_lookup_requires_both_random_credentials(self):
        response = self.client.post(reverse("identity_access:request_access"), {
            "full_name": "Status Officer",
            "official_email": "status@example.gov",
            "employee_identifier": "STATUS-1",
            "organization": self.org_a.pk,
            "designation": "Officer",
            "reason": "Official duties",
        })
        access_request = AccessRequest.objects.get(official_email="status@example.gov")
        self.assertContains(response, str(access_request.pk))
        invalid = self.client.post(reverse("identity_access:request_status"), {"request_id": access_request.pk, "status_token": "incorrect-key"})
        self.assertContains(invalid, "invalid or unavailable")

    def test_bootstrap_ias_has_no_default_password_and_refuses_duplicates(self):
        output = io.StringIO()
        call_command(
            "bootstrap_ias",
            username="ias-bootstrap",
            email="ias-bootstrap@example.gov",
            organization_code="planning",
            organization_name="Planning",
            stdout=output,
        )
        user = self.User.objects.get(username="ias-bootstrap")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(AccessGrant.objects.filter(user=user, role=AccessRole.IAS_OFFICER).exists())
        self.assertIn("/access/activate/", output.getvalue())
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_ias",
                username="ias-bootstrap",
                email="another@example.gov",
                organization_code="planning",
                organization_name="Planning",
            )
