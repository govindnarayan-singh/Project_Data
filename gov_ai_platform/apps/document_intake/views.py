from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.governance.exceptions import GovernanceApprovalError
from apps.governance.services import decide_approval_case
from apps.identity_access.permissions import can_approve_document

from .forms import DocumentDecisionForm, DocumentSubmissionForm, RevalidateDocumentForm, RetryIngestionForm
from .ingestion import retry_ingestion, worker_health
from .models import DocumentSubmission, JobState, SecurityStatus, SubmissionState
from .presentation import ingestion_presentation, validation_presentation
from .services import (
    DocumentIntakeError,
    authorized_submission_organizations,
    can_revalidate_submission,
    can_view_submission,
    quarantine_path,
    revalidate_document,
    submit_document,
)


def _visible_submission(request, submission_id):
    submission = get_object_or_404(
        DocumentSubmission.objects.select_related(
            "organization", "submitted_by", "approval_case", "matched_submission"
        ),
        pk=submission_id,
    )
    if not can_view_submission(request.user, submission):
        raise Http404
    return submission


@login_required
@require_http_methods(["GET", "POST"])
def submit(request):
    organizations = authorized_submission_organizations(request.user)
    if not organizations.exists():
        return HttpResponseForbidden("Active document-submission scope is required.")
    form = DocumentSubmissionForm(
        request.POST or None,
        request.FILES or None,
        organizations=organizations,
        user=request.user,
    )
    if request.method == "POST" and form.is_valid():
        try:
            submission = submit_document(
                actor=request.user,
                upload=form.cleaned_data["document"],
                organization=form.cleaned_data["organization"],
                claimed_identifier=form.cleaned_data["claimed_identifier"],
                claimed_source_url=form.cleaned_data["claimed_source_url"],
                source_context=form.cleaned_data["source_context"],
                ias_source_attested=form.cleaned_data.get("ias_source_attested", False),
                submission_reason=form.cleaned_data["submission_reason"],
            )
        except DocumentIntakeError as exc:
            form.add_error(None, exc.safe_message)
        else:
            if submission.state == SubmissionState.VALIDATION_FAILED:
                messages.error(request, submission.safe_failure_reason)
            else:
                messages.success(request, "The PDF passed bounded validation and is pending IAS approval.")
            return redirect("document_intake:detail", submission_id=submission.pk)
    return render(request, "document_intake/submit.html", {"form": form, "active_section": "document-submit"})


@login_required
@require_GET
def submission_list(request):
    queryset = DocumentSubmission.objects.filter(submitted_by=request.user).select_related("organization")
    page = Paginator(queryset, 25).get_page(request.GET.get("page"))
    return render(request, "document_intake/list.html", {"page_obj": page, "active_section": "document-submissions"})


@login_required
@require_GET
def detail(request, submission_id):
    submission = _visible_submission(request, submission_id)
    job = getattr(submission, "ingestion_job", None)
    can_decide = bool(
        submission.approval_case_id
        and submission.approval_case.state == "PENDING"
        and can_approve_document(request.user, submission.organization_id)
    )
    decision_form = None
    if can_decide:
        decision_form = DocumentDecisionForm(initial={
            "expected_case_version": submission.approval_case.version,
            "expected_subject_version": submission.version,
        })
    retry_form = None
    if job and job.state == JobState.RETRYABLE and can_approve_document(request.user, submission.organization_id):
        retry_form = RetryIngestionForm(initial={"expected_version": submission.version})
    revalidate_form = None
    if can_revalidate_submission(request.user, submission):
        revalidate_form = RevalidateDocumentForm(initial={"expected_version": submission.version})
    show_technical = can_approve_document(request.user, submission.organization_id)
    return render(request, "document_intake/detail.html", {
        "submission": submission,
        "job": job,
        "can_decide": can_decide,
        "decision_form": decision_form,
        "retry_form": retry_form,
        "revalidate_form": revalidate_form,
        "validation_view": validation_presentation(submission, show_technical=show_technical),
        "ingestion_view": ingestion_presentation(job),
        "show_technical": show_technical,
        "worker_health": worker_health(job=job) if show_technical and job else None,
        "active_section": "governance-approvals" if can_approve_document(request.user, submission.organization_id) else "document-submissions",
    })


def _pdf_response(request, submission_id, *, inline: bool):
    submission = _visible_submission(request, submission_id)
    if submission.security_status != SecurityStatus.PASSED:
        return HttpResponseForbidden("Only structurally validated PDFs may be served.")
    path = quarantine_path(submission)
    if not path.is_file():
        raise Http404
    response = FileResponse(path.open("rb"), content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="submission-{submission.pk}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    response["Cache-Control"] = "private, no-store"
    response["X-Frame-Options"] = "SAMEORIGIN" if inline else "DENY"
    return response


@login_required
@require_GET
def preview(request, submission_id):
    return _pdf_response(request, submission_id, inline=True)


@login_required
@require_GET
def download(request, submission_id):
    return _pdf_response(request, submission_id, inline=False)


@login_required
@require_POST
def decide(request, submission_id):
    submission = _visible_submission(request, submission_id)
    if not can_approve_document(request.user, submission.organization_id):
        return HttpResponseForbidden("IAS authority is required.")
    if not submission.approval_case_id:
        raise Http404
    form = DocumentDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "The decision form is invalid; refresh and try again.")
        return redirect("document_intake:detail", submission_id=submission.pk)
    try:
        decide_approval_case(
            request.user,
            submission.approval_case_id,
            decision=form.cleaned_data["decision"],
            reason=form.cleaned_data["reason"],
            expected_case_version=form.cleaned_data["expected_case_version"],
            expected_subject_version=form.cleaned_data["expected_subject_version"],
            correlation_id=submission.correlation_id,
        )
    except GovernanceApprovalError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The governance decision was recorded.")
    return redirect("document_intake:detail", submission_id=submission.pk)


@login_required
@require_POST
def retry(request, submission_id):
    submission = _visible_submission(request, submission_id)
    form = RetryIngestionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "The retry request is stale or invalid.")
        return redirect("document_intake:detail", submission_id=submission.pk)
    try:
        retry_ingestion(actor=request.user, submission_id=submission.pk, expected_version=form.cleaned_data["expected_version"])
    except (DocumentIntakeError, DocumentSubmission.DoesNotExist) as exc:
        messages.error(request, getattr(exc, "safe_message", "Submission not found."))
    else:
        messages.success(request, "The ingestion job was queued for another bounded attempt.")
    return redirect("document_intake:detail", submission_id=submission.pk)


@login_required
@require_POST
def revalidate(request, submission_id):
    submission = _visible_submission(request, submission_id)
    form = RevalidateDocumentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "The revalidation request is stale or invalid; refresh and try again.")
        return redirect("document_intake:detail", submission_id=submission.pk)
    try:
        result = revalidate_document(
            actor=request.user,
            submission_id=submission.pk,
            expected_version=form.cleaned_data["expected_version"],
        )
    except DocumentIntakeError as exc:
        messages.error(request, exc.safe_message)
    else:
        if result.state == SubmissionState.PENDING_IAS_APPROVAL:
            messages.success(request, "The same quarantined bytes passed the current validator and are pending IAS approval.")
        else:
            messages.error(request, result.safe_failure_reason)
    return redirect("document_intake:detail", submission_id=submission.pk)
