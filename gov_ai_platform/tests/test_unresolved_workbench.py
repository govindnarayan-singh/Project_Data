from __future__ import annotations

import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.document_intake.models import (
    DocumentSubmission,
    MalwareStatus,
    SecurityStatus,
    SourceProvenance,
    SubmissionState,
    ValidationState,
)
from apps.governance.audit import verify_audit_chain
from apps.governance.constants import GENESIS_HASH, ApprovalDecisionType, AuditAction
from apps.governance.exceptions import GovernanceApprovalError
from apps.governance.models import GovernanceAuditChain, GovernanceAuditEvent
from apps.governance.services import decide_approval_case
from apps.identity_access.models import AccessGrant, AccessRole, IASAuthorityScope, OrganizationUnit
from apps.knowledge_graph_ui.models import (
    ProposalState,
    MatchStatus,
    UnresolvedReferenceCandidateLink,
    UnresolvedReferenceWorkItem,
    WorkItemStatus,
    WorkItemType,
    WorkItemSubmission,
)
from apps.knowledge_graph_ui.workbench_services import (
    WorkbenchError,
    assign_work_item,
    claim_work_item,
    close_invalid,
    propose_resolution,
    refresh_linked_work_item_for_submission,
    release_work_item,
    submit_local_catalog_document,
    submit_work_item_upload,
    visible_work_items,
)


class WorkbenchBase:
    def setUp(self):
        super().setUp()
        GovernanceAuditChain.objects.get_or_create(pk=1, defaults={"last_sequence": 0, "head_hash": GENESIS_HASH})
        self.org_a = OrganizationUnit.objects.create(code="stage4-a", name="Finance Department")
        self.org_b = OrganizationUnit.objects.create(code="stage4-b", name="Housing Department")
        users = get_user_model()
        self.officer = users.objects.create_user("stage4-officer", password="TestOnlyPass-4401")
        self.other_officer = users.objects.create_user("stage4-other", password="TestOnlyPass-4401")
        self.cross_scope = users.objects.create_user("stage4-cross", password="TestOnlyPass-4401")
        self.ias = users.objects.create_user("stage4-ias", password="TestOnlyPass-4401")
        self.ungoverned = users.objects.create_user("stage4-none", password="TestOnlyPass-4401")
        AccessGrant.objects.create(user=self.officer, organization=self.org_a, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.other_officer, organization=self.org_a, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.cross_scope, organization=self.org_b, role=AccessRole.OFFICER)
        AccessGrant.objects.create(user=self.ias, organization=self.org_a, role=AccessRole.IAS_OFFICER)
        IASAuthorityScope.objects.create(user=self.ias, organization=self.org_a)
        self.item = UnresolvedReferenceWorkItem.objects.create(
            target_fingerprint="a" * 64,
            organization=self.org_a,
            display_identifier="FIN/2026/17",
            normalized_target_identity="fin/2026/17",
            reference_kind="GOVERNMENT_RESOLUTION",
            work_type=WorkItemType.MISSING_DOCUMENT,
            mention_count=2,
            distinct_source_document_count=1,
        )
        UnresolvedReferenceCandidateLink.objects.create(
            work_item=self.item,
            candidate_id=7001,
            source_document_id=81,
            source_page=2,
            candidate_fingerprint="b" * 64,
        )
        self.other_item = UnresolvedReferenceWorkItem.objects.create(
            target_fingerprint="c" * 64,
            organization=self.org_b,
            display_identifier="HOUSE/2026/1",
            normalized_target_identity="house/2026/1",
            reference_kind="GOVERNMENT_RESOLUTION",
            work_type=WorkItemType.MISSING_DOCUMENT,
        )


class WorkbenchAuthorizationAndWorkflowTests(WorkbenchBase, TestCase):
    def test_visibility_is_organization_scoped_and_ungranted_users_see_nothing(self):
        self.assertEqual(list(visible_work_items(self.officer)), [self.item])
        self.assertEqual(list(visible_work_items(self.cross_scope)), [self.other_item])
        self.assertFalse(visible_work_items(self.ungoverned).exists())
        self.assertEqual(list(visible_work_items(self.ias)), [self.item])

    def test_claim_release_assignment_stale_version_and_audit(self):
        claimed = claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        self.assertEqual(claimed.status, WorkItemStatus.CLAIMED)
        self.assertEqual(claimed.claimed_by_id, self.officer.pk)
        with self.assertRaises(WorkbenchError) as stale:
            release_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        self.assertEqual(stale.exception.code, "STALE_WORK_ITEM")
        assigned = assign_work_item(
            actor=self.ias,
            item_id=self.item.pk,
            assignee_id=self.other_officer.pk,
            expected_version=claimed.version,
        )
        self.assertEqual(assigned.assigned_to_id, self.other_officer.pk)
        released = release_work_item(actor=self.ias, item_id=self.item.pk, expected_version=assigned.version)
        self.assertIsNone(released.claimed_by_id)
        self.assertTrue(verify_audit_chain().valid)
        self.assertEqual(
            list(GovernanceAuditEvent.objects.filter(
                resource_type="knowledge_graph_ui.UnresolvedReferenceWorkItem",
                resource_id=str(self.item.pk),
            ).order_by("sequence").values_list("action", flat=True)),
            [
                AuditAction.UNRESOLVED_WORK_ITEM_CLAIMED,
                AuditAction.UNRESOLVED_WORK_ITEM_ASSIGNED,
                AuditAction.UNRESOLVED_WORK_ITEM_RELEASED,
            ],
        )

    def test_cross_scope_and_non_owner_actions_fail_closed(self):
        with self.assertRaises(WorkbenchError) as hidden:
            claim_work_item(actor=self.cross_scope, item_id=self.item.pk, expected_version=1)
        self.assertEqual(hidden.exception.code, "WORK_ITEM_NOT_FOUND")
        claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        with self.assertRaises(WorkbenchError) as owner:
            release_work_item(actor=self.other_officer, item_id=self.item.pk, expected_version=2)
        self.assertEqual(owner.exception.code, "NOT_CLAIM_OWNER")

    def test_ias_invalid_closure_preserves_candidate_link(self):
        closed = close_invalid(
            actor=self.ias,
            item_id=self.item.pk,
            expected_version=1,
            reason="Parser captured a heading rather than a target document.",
        )
        self.assertEqual(closed.status, WorkItemStatus.CLOSED_INVALID)
        self.assertEqual(closed.candidate_links.count(), 1)
        self.assertTrue(GovernanceAuditEvent.objects.filter(action=AuditAction.WORK_ITEM_CLOSED_INVALID).exists())

    def _submission(self, suffix="1"):
        return DocumentSubmission.objects.create(
            organization=self.org_a,
            submitted_by=self.officer,
            submitter_role_snapshot=AccessRole.OFFICER,
            original_filename=f"candidate-{suffix}.pdf",
            storage_key=f"stage4-test-{suffix}/original.pdf",
            sha256=suffix.rjust(64, "0"),
            byte_size=100,
            declared_mime="application/pdf",
            detected_mime="application/pdf",
            state=SubmissionState.PENDING_IAS_APPROVAL,
            validation_state=ValidationState.PASSED,
            security_status=SecurityStatus.PASSED,
            malware_status=MalwareStatus.NOT_PERFORMED,
            source_provenance=SourceProvenance.OFFICER_PROVIDED_UNVERIFIED,
            submission_reason="Stage 4 integration fixture.",
        )

    @patch("apps.knowledge_graph_ui.workbench_services.submit_document")
    def test_stage3_upload_linkage_and_active_submission_spam_guard(self, stage3_submit):
        submission = self._submission()
        stage3_submit.return_value = submission
        claimed = claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        result = submit_work_item_upload(
            actor=self.officer,
            item_id=self.item.pk,
            expected_version=claimed.version,
            upload=object(),
            claimed_identifier="FIN/2026/17",
            claimed_source_url="https://example.gov.in/official.pdf",
            submission_reason="Found on an official department page.",
        )
        self.assertEqual(result.pk, submission.pk)
        link = WorkItemSubmission.objects.get(submission=submission)
        self.assertEqual(link.work_item_id, self.item.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, WorkItemStatus.DOCUMENT_SUBMITTED)
        self.assertEqual(stage3_submit.call_args.kwargs["origin"], "UNRESOLVED_REFERENCE")
        self.assertEqual(stage3_submit.call_args.kwargs["origin_reference"], str(self.item.pk))
        with self.assertRaises(WorkbenchError) as duplicate:
            submit_work_item_upload(
                actor=self.officer,
                item_id=self.item.pk,
                expected_version=self.item.version,
                upload=object(),
                claimed_identifier="FIN/2026/17",
                claimed_source_url="",
                submission_reason="Repeated submission.",
            )
        self.assertEqual(duplicate.exception.code, "ACTIVE_SUBMISSION_EXISTS")

    def test_local_catalog_handoff_rejects_unoffered_identifier_before_path_access(self):
        self.item.local_match_status = MatchStatus.EXACT
        self.item.local_candidate_ids = [88]
        self.item.local_catalog_id = 88
        self.item.save(update_fields=("local_match_status", "local_candidate_ids", "local_catalog_id"))
        claimed = claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        with self.assertRaises(WorkbenchError) as invalid:
            submit_local_catalog_document(
                actor=self.officer,
                item_id=self.item.pk,
                expected_version=claimed.version,
                catalog_id=99,
                submission_reason="Wrong catalog identifier.",
            )
        self.assertEqual(invalid.exception.code, "LOCAL_MATCH_NOT_ELIGIBLE")

    @patch("apps.knowledge_graph_ui.workbench_services.refresh_work_item_from_graph")
    def test_ingestion_callback_refreshes_only_the_linked_work_item(self, refresh):
        submission = self._submission("2")
        WorkItemSubmission.objects.create(
            work_item=self.item,
            submission=submission,
            created_by=self.officer,
        )
        refresh.return_value = self.item
        self.assertEqual(refresh_linked_work_item_for_submission(submission), self.item)
        refresh.assert_called_once_with(self.item.pk)

    @patch("apps.knowledge_graph_ui.workbench_services.document_summaries")
    def test_incorrect_human_target_is_rejected_before_governance_case(self, documents):
        documents.return_value = [{
            "id": 55,
            "file_name": "wrong.pdf",
            "department": "Finance Department",
            "gr_number": "OTHER/2026/9",
        }]
        claimed = claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        with self.assertRaises(WorkbenchError) as mismatch:
            propose_resolution(
                actor=self.officer,
                item_id=self.item.pk,
                expected_version=claimed.version,
                target_document_id=55,
                reason="Incorrect target test.",
            )
        self.assertEqual(mismatch.exception.code, "TARGET_IDENTITY_MISMATCH")

    @patch("apps.knowledge_graph_ui.workbench_services.document_summaries")
    @patch("apps.knowledge_graph_ui.workbench_services._validate_target")
    def test_officer_proposal_does_not_change_graph_until_ias_approval(self, validate_target, document_summaries_mock):
        target = {"id": 44, "file_name": "target.pdf", "department": "Finance Department", "gr_number": "FIN/2026/17"}
        validate_target.return_value = target
        document_summaries_mock.return_value = [target]
        claimed = claim_work_item(actor=self.officer, item_id=self.item.pk, expected_version=1)
        proposal = propose_resolution(
            actor=self.officer,
            item_id=self.item.pk,
            expected_version=claimed.version,
            target_document_id=44,
            reason="Candidate identifier and dated source evidence match this corpus document.",
        )
        self.assertEqual(proposal.state, ProposalState.PENDING)
        self.assertEqual(proposal.relation_ids, [])
        with patch("apps.knowledge_graph_ui.governance.confirm_human_resolution", return_value=[901]) as confirm:
            decide_approval_case(
                self.ias,
                proposal.approval_case_id,
                decision=ApprovalDecisionType.APPROVE,
                reason="Evidence and target identity verified.",
                expected_case_version=proposal.approval_case.version,
                expected_subject_version=proposal.version,
                correlation_id=proposal.correlation_id,
            )
        confirm.assert_called_once()
        proposal.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(proposal.state, ProposalState.APPROVED)
        self.assertEqual(proposal.relation_ids, [901])
        self.assertEqual(self.item.status, WorkItemStatus.RESOLVED_HUMAN)
        self.assertTrue(verify_audit_chain().valid)

    @override_settings(KG_REFERENCE_IAS_SELF_CONFIRM_ALLOWED=False)
    @patch("apps.knowledge_graph_ui.workbench_services.document_summaries")
    @patch("apps.knowledge_graph_ui.workbench_services._validate_target")
    def test_ias_self_approval_is_explicitly_prohibited_by_default(self, validate_target, document_summaries_mock):
        target = {"id": 44, "file_name": "target.pdf", "department": "Finance Department", "gr_number": "FIN/2026/17"}
        validate_target.return_value = target
        document_summaries_mock.return_value = [target]
        proposal = propose_resolution(
            actor=self.ias,
            item_id=self.item.pk,
            expected_version=1,
            target_document_id=44,
            reason="IAS-authored proposal for maker-checker test.",
        )
        with self.assertRaises(GovernanceApprovalError) as denied:
            decide_approval_case(
                self.ias,
                proposal.approval_case_id,
                decision=ApprovalDecisionType.APPROVE,
                reason="Attempted self approval.",
                expected_case_version=proposal.approval_case.version,
                expected_subject_version=proposal.version,
            )
        self.assertEqual(denied.exception.code, "SELF_APPROVAL")

    def test_list_and_detail_idor_boundaries(self):
        self.assertEqual(self.client.get(reverse("knowledge_graph_ui:unresolved")).status_code, 302)
        self.client.force_login(self.officer)
        listing = self.client.get(reverse("knowledge_graph_ui:unresolved"), {"q": "FIN", "status": "OPEN"})
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "FIN/2026/17")
        self.assertNotContains(listing, "HOUSE/2026/1")
        self.assertEqual(self.client.get(reverse("knowledge_graph_ui:work_item_detail", args=[self.other_item.pk])).status_code, 404)


class WorkbenchPostgresConcurrencyTests(WorkbenchBase, TransactionTestCase):
    def test_competing_claims_have_exactly_one_winner(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock semantics are required")
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(user_id):
            close_old_connections()
            user = get_user_model().objects.get(pk=user_id)
            barrier.wait()
            try:
                claim_work_item(actor=user, item_id=self.item.pk, expected_version=1)
            except WorkbenchError as exc:
                result = exc.code
            else:
                result = "CLAIMED"
            finally:
                close_old_connections()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt, args=(user.pk,)) for user in (self.officer, self.other_officer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(outcomes.count("CLAIMED"), 1)
        self.assertEqual(len(outcomes), 2)
        self.assertIn(next(value for value in outcomes if value != "CLAIMED"), {"STALE_WORK_ITEM", "ALREADY_CLAIMED"})
