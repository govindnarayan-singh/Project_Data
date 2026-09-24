from __future__ import annotations

import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.governance.audit import audit_health, record_governance_event, sanitize_metadata, verify_audit_chain
from apps.governance.constants import (
    ApprovalCaseState,
    ApprovalDecisionType,
    AuditAction,
    AuditCategory,
    AuditOrigin,
    AuditOutcome,
)
from apps.governance.exceptions import AuditValidationError, AuditWriteError, GovernanceApprovalError
from apps.governance.models import (
    GovernanceApprovalCase,
    GovernanceApprovalDecision,
    GovernanceAuditChain,
    GovernanceAuditEvent,
)
from apps.governance.policies import ApprovalPolicy, SubjectSnapshot, temporary_policy
from apps.governance.services import create_approval_case, decide_approval_case
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
from apps.identity_access.permissions import can_manage_organization
from apps.identity_access.services import (
    AccessDecisionError,
    activate_account,
    approve_access_request,
    change_officer_grant_state,
    create_activation_token,
    reject_access_request,
)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ACCESS_ALLOWED_EMAIL_DOMAINS=(),
    ACCESS_ALLOWED_ORGANIZATION_CODES=(),
    ACCESS_REQUEST_SOURCE_LIMIT=1,
    ACCESS_REQUEST_EMAIL_LIMIT=100,
    ACCESS_REQUEST_IDENTITY_LIMIT=100,
    ACCESS_LOGIN_SOURCE_LIMIT=100,
    ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT=100,
)
class GovernanceFoundationTests(TestCase):
    def setUp(self):
        GovernanceAuditChain.objects.get_or_create(pk=1)
        User = get_user_model()
        self.org_a = OrganizationUnit.objects.create(code="gov-a", name="Governance A")
        self.org_b = OrganizationUnit.objects.create(code="gov-b", name="Governance B")
        self.ias_a = User.objects.create_user("gov-ias-a", "gov-ias-a@example.gov", "TestOnlyPass-9321")
        self.ias_b = User.objects.create_user("gov-ias-b", "gov-ias-b@example.gov", "TestOnlyPass-9321")
        self.maker = User.objects.create_user("gov-maker", "gov-maker@example.gov", "TestOnlyPass-9321")
        self.officer = User.objects.create_user("gov-officer", "gov-officer@example.gov", "TestOnlyPass-9321")
        AccessGrant.objects.create(user=self.ias_a, organization=self.org_a, role=AccessRole.IAS_OFFICER)
        AccessGrant.objects.create(user=self.ias_b, organization=self.org_b, role=AccessRole.IAS_OFFICER)
        AccessGrant.objects.create(user=self.maker, organization=self.org_a, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.officer, organization=self.org_a, role=AccessRole.OFFICER)
        IASAuthorityScope.objects.create(user=self.ias_a, organization=self.org_a)
        IASAuthorityScope.objects.create(user=self.ias_b, organization=self.org_b)
        self.subjects = {
            "subject-a": SubjectSnapshot(version=7, organization_id=self.org_a.pk, digest="a" * 64, state="READY"),
            "subject-b": SubjectSnapshot(version=3, organization_id=self.org_b.pk, digest="b" * 64, state="READY"),
        }
        policy = ApprovalPolicy(
            workflow_type="ISOLATED_TEST_WORKFLOW",
            required_reviewer_role=AccessRole.IAS_OFFICER,
            prohibit_self_approval=True,
            read_subject=lambda subject_id: self.subjects[subject_id],
            can_review_organization=can_manage_organization,
            allowed_decisions=frozenset(ApprovalDecisionType.values),
        )
        self.policy_context = temporary_policy(policy)
        self.policy_context.__enter__()
        self.addCleanup(self.policy_context.__exit__, None, None, None)

    def make_case(self, *, subject_id="subject-a", submitter=None):
        snapshot = self.subjects[subject_id]
        organization = self.org_a if snapshot.organization_id == self.org_a.pk else self.org_b
        return create_approval_case(
            workflow_type="ISOLATED_TEST_WORKFLOW",
            organization=organization,
            submitted_by=submitter or self.maker,
            subject_type="tests.IsolatedSubject",
            subject_id=subject_id,
            subject_version=snapshot.version,
            subject_digest=snapshot.digest,
        )

    def make_access_request(self, suffix: str, *, organization=None, requester=None):
        email = f"{suffix}@example.gov"
        return AccessRequest.objects.create(
            full_name=f"Officer {suffix}",
            official_email=email,
            normalized_email=email,
            employee_identifier=f"EMP-{suffix}",
            normalized_identity=f"emp{suffix}",
            organization=organization or self.org_a,
            designation="Officer",
            reason="Governance integration test",
            state=AccessRequestState.PENDING_IAS_APPROVAL,
            validation_outcome=ValidationOutcome.ALLOW_TO_QUEUE,
            source_fingerprint="a" * 64,
            status_token_digest="b" * 64,
            requester_user=requester,
            version=3,
            expires_at=timezone.now() + timedelta(days=2),
        )

    def make_event(self, organization, resource_id: str, *, reason="bounded reason"):
        return record_governance_event(
            category=AuditCategory.SYSTEM,
            action=AuditAction.ACCESS_REQUEST_STATE_IMPORTED,
            outcome=AuditOutcome.SUCCESS,
            organization_id=organization.pk,
            resource_type="tests.Resource",
            resource_id=resource_id,
            reason_code="TEST_EVENT",
            reason=reason,
        )

    def test_unknown_workflow_fails_closed(self):
        with self.assertRaisesMessage(GovernanceApprovalError, "Unknown approval workflow") as caught:
            create_approval_case(
                workflow_type="UNKNOWN",
                organization=self.org_a,
                submitted_by=self.maker,
                subject_type="tests.Subject",
                subject_id="subject-a",
                subject_version=7,
                subject_digest="a" * 64,
            )
        self.assertEqual(caught.exception.code, "UNKNOWN_WORKFLOW")
        self.assertFalse(GovernanceApprovalCase.objects.exists())

    def test_generic_case_approve_and_reject_are_versioned_and_append_only(self):
        approved_case = self.make_case()
        decision = decide_approval_case(
            self.ias_a,
            approved_case.pk,
            decision=ApprovalDecisionType.APPROVE,
            reason="Verified maker-checker approval",
            expected_case_version=1,
            expected_subject_version=7,
        )
        approved_case.refresh_from_db()
        self.assertEqual(approved_case.state, ApprovalCaseState.APPROVED)
        self.assertEqual(approved_case.version, 2)
        self.assertEqual(decision.case_version_seen, 1)
        self.assertEqual(decision.subject_version_seen, 7)
        with self.assertRaisesMessage(GovernanceApprovalError, "final state"):
            decide_approval_case(
                self.ias_a,
                approved_case.pk,
                decision=ApprovalDecisionType.REJECT,
                reason="repeat",
                expected_case_version=2,
                expected_subject_version=7,
            )
        with self.assertRaises(AuditValidationError):
            GovernanceApprovalDecision.objects.filter(pk=decision.pk).update(reason="rewrite")
        with self.assertRaises(AuditValidationError):
            decision.delete()

        rejected_case = self.make_case(subject_id="subject-b", submitter=self.ias_a)
        rejected = decide_approval_case(
            self.ias_b,
            rejected_case.pk,
            decision=ApprovalDecisionType.REJECT,
            reason="Subject did not satisfy policy",
            expected_case_version=1,
            expected_subject_version=3,
        )
        rejected_case.refresh_from_db()
        self.assertEqual(rejected_case.state, ApprovalCaseState.REJECTED)
        self.assertEqual(rejected.decision, ApprovalDecisionType.REJECT)

    def test_maker_checker_role_scope_and_stale_guards(self):
        self_case = self.make_case(submitter=self.ias_a)
        with self.assertRaisesMessage(GovernanceApprovalError, "self-approval"):
            decide_approval_case(self.ias_a, self_case.pk, decision="APPROVE", reason="self", expected_case_version=1, expected_subject_version=7)
        normal_case = self.make_case()
        with self.assertRaisesMessage(GovernanceApprovalError, "reviewer role"):
            decide_approval_case(self.officer, normal_case.pk, decision="APPROVE", reason="wrong role", expected_case_version=1, expected_subject_version=7)
        with self.assertRaisesMessage(GovernanceApprovalError, "outside reviewer scope"):
            decide_approval_case(self.ias_b, normal_case.pk, decision="APPROVE", reason="wrong org", expected_case_version=1, expected_subject_version=7)
        with self.assertRaisesMessage(GovernanceApprovalError, "approval case changed"):
            decide_approval_case(self.ias_a, normal_case.pk, decision="APPROVE", reason="stale", expected_case_version=2, expected_subject_version=7)
        self.subjects["subject-a"] = SubjectSnapshot(version=8, organization_id=self.org_a.pk, digest="c" * 64)
        with self.assertRaisesMessage(GovernanceApprovalError, "subject changed"):
            decide_approval_case(self.ias_a, normal_case.pk, decision="APPROVE", reason="stale subject", expected_case_version=1, expected_subject_version=7)
        normal_case.refresh_from_db()
        self.assertEqual(normal_case.state, ApprovalCaseState.PENDING)
        self.assertFalse(normal_case.decisions.exists())

    def test_forced_audit_failure_rolls_back_generic_decision(self):
        case = self.make_case()
        with patch("apps.governance.services.record_governance_event", side_effect=AuditWriteError("forced")):
            with self.assertRaisesMessage(GovernanceApprovalError, "could not be recorded safely"):
                decide_approval_case(self.ias_a, case.pk, decision="APPROVE", reason="verified", expected_case_version=1, expected_subject_version=7)
        case.refresh_from_db()
        self.assertEqual(case.state, ApprovalCaseState.PENDING)
        self.assertEqual(case.version, 1)
        self.assertFalse(case.decisions.exists())

    def test_metadata_privacy_bounds_reason_redaction_and_html_escaping(self):
        with self.assertRaises(AuditValidationError):
            sanitize_metadata({"password": "secret"})
        with self.assertRaises(AuditValidationError):
            sanitize_metadata({"risk_flags": ["password=secret"]})
        with self.assertRaises(AuditValidationError):
            sanitize_metadata({"risk_flags": [{"nested": "value"}]})
        with self.assertRaises(AuditValidationError):
            sanitize_metadata({"validation_outcome": "x" * 257})
        event = self.make_event(self.org_a, "injection", reason="<script>alert(1)</script>\npassword=topsecret")
        self.assertIn("password=[REDACTED]", event.reason)
        self.assertNotIn("topsecret", event.reason)
        self.client.force_login(self.ias_a)
        response = self.client.get(reverse("governance:audit_detail", args=[event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_correlation_id_is_server_generated_and_client_value_is_ignored(self):
        supplied = str(uuid.uuid4())
        response = self.client.get(reverse("identity_access:login"), HTTP_X_REQUEST_ID=supplied)
        generated = response["X-Request-ID"]
        self.assertNotEqual(generated, supplied)
        uuid.UUID(generated)

    def test_audit_ui_is_paginated_scoped_and_idor_safe(self):
        event_a = self.make_event(self.org_a, "scope-a")
        event_b = self.make_event(self.org_b, "scope-b")
        self.client.force_login(self.ias_a)
        response = self.client.get(reverse("governance:audit_list"), {"organization": self.org_b.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "scope-a")
        self.assertNotContains(response, "scope-b")
        self.assertEqual(response.context["page_obj"].paginator.per_page, 50)
        self.assertEqual(self.client.get(reverse("governance:audit_detail", args=[event_a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("governance:audit_detail", args=[event_b.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("governance:approval_case_list")).status_code, 200)
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get(reverse("governance:audit_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("governance:approval_case_list")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("governance:audit_list")).status_code, 302)

    def test_remote_noise_and_ordinary_reads_do_not_flood_audit(self):
        initial = GovernanceAuditEvent.objects.count()
        for _ in range(3):
            self.client.post(reverse("identity_access:login"), {"identifier": "unknown", "password": "bad"})
        self.assertEqual(GovernanceAuditEvent.objects.count(), initial)
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get(reverse("search_ui:search")).status_code, 200)
        self.assertEqual(GovernanceAuditEvent.objects.count(), initial)
        self.client.logout()
        request_url = reverse("identity_access:request_access")
        first = self.client.post(request_url, {
            "full_name": "Flood One",
            "official_email": "flood-one@example.gov",
            "employee_identifier": "FLOOD-ONE",
            "organization": self.org_a.pk,
            "reason": "Official access",
        }, REMOTE_ADDR="198.51.100.240")
        self.assertEqual(first.status_code, 200)
        after_first = GovernanceAuditEvent.objects.count()
        second = self.client.post(request_url, {
            "full_name": "Flood Two",
            "official_email": "flood-two@example.gov",
            "employee_identifier": "FLOOD-TWO",
            "organization": self.org_a.pk,
            "reason": "Official access",
        }, REMOTE_ADDR="198.51.100.240")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(after_first, initial + 1)
        self.assertEqual(GovernanceAuditEvent.objects.count(), after_first)
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.ACCESS_REQUEST_SUBMITTED).exists())

    def test_stage1_approval_rejection_activation_and_grant_change_are_audited(self):
        correlation = uuid.uuid4()
        approval_request = self.make_access_request("governed-approve")
        approved = approve_access_request(self.ias_a, approval_request.pk, expected_version=3, reason="verified", correlation_id=correlation)
        approval_events = GovernanceAuditEvent.objects.filter(correlation_id=correlation)
        self.assertEqual(approval_events.count(), 2)
        self.assertSetEqual(set(approval_events.values_list("action", flat=True)), {AuditAction.ACCESS_REQUEST_APPROVED, AuditAction.ACCESS_GRANT_CREATED})
        self.assertFalse(AccessGrant.objects.filter(user_id=approved.user_id, role=AccessRole.IAS_OFFICER).exists())

        rejection_request = self.make_access_request("governed-reject")
        reject_access_request(self.ias_a, rejection_request.pk, expected_version=3, reason="not verified")
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.ACCESS_REQUEST_REJECTED, resource_id=str(rejection_request.pk)).exists())
        self.assertFalse(ActivationToken.objects.filter(access_request=rejection_request).exists())

        activated_user = get_user_model().objects.get(pk=approved.user_id)
        raw_token = approved.activation_token
        activate_account(activated_user.pk, raw_token, "ActivatedPass-9321")
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.ACTIVATION_CONSUMED, actor_user_id=activated_user.pk).exists())
        grant = AccessGrant.objects.get(user=activated_user, role=AccessRole.OFFICER)
        change_officer_grant_state(self.ias_a, grant.pk, expected_version=grant.version, new_state=AccessGrantState.SUSPENDED, reason="security review")
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.ACCESS_GRANT_SUSPENDED, resource_id=str(grant.pk)).exists())
        grant.refresh_from_db()
        change_officer_grant_state(self.ias_a, grant.pk, expected_version=grant.version, new_state=AccessGrantState.REVOKED, reason="confirmed revocation")
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.ACCESS_GRANT_REVOKED, resource_id=str(grant.pk)).exists())

        serialized_audit = json.dumps(list(GovernanceAuditEvent.objects.values()), default=str)
        forbidden_values = [
            raw_token,
            "ActivatedPass-9321",
            settings.SECRET_KEY,
            settings.DATABASES["default"].get("PASSWORD", ""),
        ]
        for secret in forbidden_values:
            if secret:
                self.assertNotIn(secret, serialized_audit)

    def test_ias_bootstrap_is_audited_without_secret_metadata(self):
        output = io.StringIO()
        call_command(
            "bootstrap_ias",
            username="governance-bootstrap",
            email="governance-bootstrap@example.gov",
            organization_code="governance-bootstrap",
            organization_name="Governance Bootstrap",
            stdout=output,
        )
        user = get_user_model().objects.get(username="governance-bootstrap")
        event = GovernanceAuditEvent.objects.get(action=AuditAction.IAS_BOOTSTRAPPED, resource_id=str(user.pk))
        serialized = json.dumps({"reason": event.reason, "metadata": event.metadata}).casefold()
        self.assertNotIn("password", serialized)
        self.assertNotIn("activation", serialized)
        self.assertNotIn("token", serialized)

    def test_stage1_mutation_rolls_back_when_audit_fails(self):
        request = self.make_access_request("audit-failure")
        with patch("apps.identity_access.services.record_governance_event", side_effect=AuditWriteError("forced")):
            with self.assertRaisesMessage(AccessDecisionError, "could not be completed safely"):
                approve_access_request(self.ias_a, request.pk, expected_version=3, reason="verified")
        request.refresh_from_db()
        self.assertEqual(request.state, AccessRequestState.PENDING_IAS_APPROVAL)
        self.assertEqual(request.version, 3)
        self.assertFalse(get_user_model().objects.filter(email=request.normalized_email).exists())

    def test_historical_import_is_dry_run_first_and_idempotent(self):
        request = self.make_access_request("history")
        transition = AccessRequestTransition.objects.create(
            request=request,
            old_state=AccessRequestState.PENDING_VALIDATION,
            new_state=AccessRequestState.PENDING_IAS_APPROVAL,
            reason="validated",
            version=3,
        )
        dry = io.StringIO()
        call_command("import_stage1_audit_history", dry_run=True, stdout=dry)
        self.assertEqual(json.loads(dry.getvalue())["imported"], 0)
        self.assertFalse(GovernanceAuditEvent.objects.filter(source_identifier=f"identity_access.transition:{transition.pk}").exists())
        applied = io.StringIO()
        call_command("import_stage1_audit_history", apply=True, stdout=applied)
        self.assertEqual(json.loads(applied.getvalue())["imported"], 1)
        post_cutover = AccessRequestTransition.objects.create(
            request=request,
            old_state=AccessRequestState.PENDING_IAS_APPROVAL,
            new_state=AccessRequestState.PENDING_IAS_APPROVAL,
            reason="live-era transition",
            version=4,
        )
        repeated = io.StringIO()
        call_command("import_stage1_audit_history", apply=True, stdout=repeated)
        self.assertEqual(json.loads(repeated.getvalue())["imported"], 0)
        self.assertFalse(
            GovernanceAuditEvent.objects.filter(
                source_identifier=f"identity_access.transition:{post_cutover.pk}"
            ).exists()
        )
        event = GovernanceAuditEvent.objects.get(source_identifier=f"identity_access.transition:{transition.pk}")
        self.assertEqual(event.origin, AuditOrigin.HISTORICAL_STAGE1_IMPORT)
        self.assertEqual(event.source_occurred_at, transition.created_at)

    def test_verification_and_health_commands_are_concise(self):
        self.make_event(self.org_a, "health")
        result = verify_audit_chain()
        self.assertTrue(result.valid)
        health = audit_health()
        self.assertTrue(health["chain_head_consistent"])
        verify_output = io.StringIO()
        call_command("verify_audit_log", stdout=verify_output)
        self.assertIn("PASS", verify_output.getvalue())
        health_output = io.StringIO()
        call_command("audit_health", stdout=health_output)
        self.assertEqual(json.loads(health_output.getvalue())["event_count"], 1)
