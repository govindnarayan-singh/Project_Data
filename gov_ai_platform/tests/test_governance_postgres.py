from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from apps.governance.audit import record_governance_event, verify_audit_chain
from apps.governance.constants import (
    ApprovalCaseState,
    ApprovalDecisionType,
    AuditAction,
    AuditCategory,
    AuditOutcome,
    GENESIS_HASH,
)
from apps.governance.exceptions import AuditValidationError
from apps.governance.models import (
    GovernanceApprovalCase,
    GovernanceApprovalDecision,
    GovernanceAuditChain,
    GovernanceAuditEvent,
)
from apps.identity_access.models import OrganizationUnit


class GovernancePostgreSQLTests(TransactionTestCase):
    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise AssertionError("Governance database enforcement tests must run against PostgreSQL.")

    def setUp(self):
        GovernanceAuditChain.objects.get_or_create(pk=1, defaults={"last_sequence": 0, "head_hash": GENESIS_HASH})
        self.org = OrganizationUnit.objects.create(code="audit-pg", name="Audit PostgreSQL")
        self.user = get_user_model().objects.create_user("audit-pg-user", "audit-pg@example.gov", "TestOnlyPass-9321")

    def record_event(self, suffix: str):
        return record_governance_event(
            category=AuditCategory.SYSTEM,
            action=AuditAction.ACCESS_REQUEST_STATE_IMPORTED,
            outcome=AuditOutcome.SUCCESS,
            actor=self.user,
            organization_id=self.org.pk,
            resource_type="tests.PostgreSQLResource",
            resource_id=suffix,
            reason_code="POSTGRES_TEST",
        )

    def test_audit_insert_succeeds_and_all_normal_mutations_are_rejected(self):
        event = self.record_event("immutable")
        self.assertEqual(event.sequence, 1)
        event.reason = "rewrite"
        with self.assertRaises(AuditValidationError):
            event.save()
        with self.assertRaises(AuditValidationError):
            GovernanceAuditEvent.objects.filter(pk=event.pk).update(reason="rewrite")
        with self.assertRaises(AuditValidationError):
            GovernanceAuditEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE governance_governanceauditevent SET reason = %s WHERE event_id = %s", ("rewrite", event.pk))
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM governance_governanceauditevent WHERE event_id = %s", (event.pk,))
        second = self.record_event("insert-still-works")
        self.assertEqual(second.sequence, 2)

    def test_decision_records_are_append_only_in_application_and_postgresql(self):
        case = GovernanceApprovalCase.objects.create(
            workflow_type="TEST",
            organization=self.org,
            submitted_by=self.user,
            subject_type="tests.Subject",
            subject_id="one",
            subject_version=1,
            state=ApprovalCaseState.PENDING,
        )
        decision = GovernanceApprovalDecision(
            case=case,
            actor=self.user,
            actor_role_snapshot="IAS_OFFICER",
            organization_id_snapshot=self.org.pk,
            decision=ApprovalDecisionType.APPROVE,
            reason="verified",
            subject_version_seen=1,
            case_version_seen=1,
        )
        decision._governance_insert_allowed = True
        decision.save(force_insert=True)
        with self.assertRaises(AuditValidationError):
            GovernanceApprovalDecision.objects.filter(pk=decision.pk).update(reason="rewrite")
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE governance_governanceapprovaldecision SET reason = %s WHERE id = %s", ("rewrite", decision.pk))
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM governance_governanceapprovaldecision WHERE id = %s", (decision.pk,))

    def test_genesis_and_previous_hash_chain_verify(self):
        first = self.record_event("first")
        second = self.record_event("second")
        self.assertEqual(first.previous_hash, GENESIS_HASH)
        self.assertEqual(second.previous_hash, first.event_hash)
        result = verify_audit_chain()
        self.assertTrue(result.valid)
        self.assertEqual(result.events_checked, 2)
        self.assertEqual(result.first_sequence, 1)
        self.assertEqual(result.last_sequence, 2)

    def test_concurrent_inserts_form_one_linear_chain(self):
        def insert(index: int):
            close_old_connections()
            try:
                event = record_governance_event(
                    category=AuditCategory.SYSTEM,
                    action=AuditAction.ACCESS_REQUEST_STATE_IMPORTED,
                    outcome=AuditOutcome.SUCCESS,
                    organization_id=self.org.pk,
                    resource_type="tests.ConcurrentResource",
                    resource_id=str(index),
                    reason_code="CONCURRENCY_TEST",
                )
                return event.sequence
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=4) as pool:
            sequences = list(pool.map(insert, range(12)))
        self.assertEqual(sorted(sequences), list(range(1, 13)))
        self.assertEqual(GovernanceAuditEvent.objects.values("previous_hash").distinct().count(), 12)
        result = verify_audit_chain()
        self.assertTrue(result.valid)
        self.assertEqual(result.events_checked, 12)

    def test_verifier_detects_modified_event(self):
        event = self.record_event("tamper")
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE governance_governanceauditevent DISABLE TRIGGER governance_audit_event_immutable")
            try:
                cursor.execute("UPDATE governance_governanceauditevent SET reason = %s WHERE event_id = %s", ("tampered", event.pk))
            finally:
                cursor.execute("ALTER TABLE governance_governanceauditevent ENABLE TRIGGER governance_audit_event_immutable")
        result = verify_audit_chain()
        self.assertFalse(result.valid)
        self.assertEqual(result.first_broken_sequence, 1)
        self.assertEqual(result.reason, "EVENT_HASH_MISMATCH")

    def test_verifier_detects_missing_sequence(self):
        first = self.record_event("first")
        self.record_event("second")
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE governance_governanceauditevent DISABLE TRIGGER governance_audit_event_immutable")
            try:
                cursor.execute("DELETE FROM governance_governanceauditevent WHERE event_id = %s", (first.pk,))
            finally:
                cursor.execute("ALTER TABLE governance_governanceauditevent ENABLE TRIGGER governance_audit_event_immutable")
        result = verify_audit_chain()
        self.assertFalse(result.valid)
        self.assertEqual(result.first_broken_sequence, 1)
        self.assertEqual(result.reason, "SEQUENCE_GAP")

    def test_verifier_detects_chain_head_mismatch(self):
        self.record_event("head")
        GovernanceAuditChain.objects.filter(pk=1).update(last_sequence=99)
        result = verify_audit_chain()
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "CHAIN_HEAD_SEQUENCE_MISMATCH")
