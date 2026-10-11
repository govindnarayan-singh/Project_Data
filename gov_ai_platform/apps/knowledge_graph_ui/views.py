from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, When
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.document_intake.services import DocumentIntakeError
from apps.governance.exceptions import GovernanceApprovalError
from apps.governance.services import decide_approval_case
from apps.identity_access.models import AccessGrant, AccessGrantState, AccessRole
from apps.identity_access.permissions import can_approve_review

from knowledge_engine.graph.explorer import (
    ExplorerValidationError,
    GraphExplorerService,
    GraphFilters,
)
from knowledge_engine.portal import PortalRepository

from .forms import (
    AssignmentForm,
    CloseInvalidForm,
    LocalCatalogSubmissionForm,
    ResolutionDecisionForm,
    ResolutionProposalForm,
    VersionedActionForm,
    WorkItemUploadForm,
)
from .models import (
    ACTIVE_WORK_ITEM_STATUSES,
    MatchStatus,
    ProposalState,
    UnresolvedReferenceCandidateLink,
    WorkItemPriority,
    WorkItemStatus,
    WorkItemType,
)
from .workbench_services import (
    WorkbenchError,
    assign_work_item,
    claim_work_item,
    close_invalid,
    document_summaries,
    evidence_for_work_item,
    get_visible_work_item,
    local_catalog_record,
    propose_resolution,
    release_work_item,
    resolve_work_item_automatically,
    submit_local_catalog_document,
    submit_work_item_upload,
    visible_work_items,
)


def get_portal_repository():
    return PortalRepository()


def get_explorer_service():
    return GraphExplorerService()


@require_GET
def overview(request):
    summary = get_explorer_service().get_graph_summary()
    summary["graph_coverage_percent"] = summary["graph_coverage"] * 100
    return render(request, "knowledge_graph/overview.html", {
        "graph": summary,
        "api": {
            "summary": reverse("knowledge_graph_ui:api_summary"),
            "graph": reverse("knowledge_graph_ui:api_graph"),
            "search": reverse("knowledge_graph_ui:api_search"),
            "components": reverse("knowledge_graph_ui:api_components"),
            "neighborhood_template": reverse("knowledge_graph_ui:api_neighborhood", args=[0]),
            "node_template": reverse("knowledge_graph_ui:api_node_detail", args=["node-placeholder"]),
            "edge_template": reverse("knowledge_graph_ui:api_edge_detail", args=[0]),
        },
        "active_section": "graph", "context_mode": "graph",
    })


def _values(request, name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in request.GET.getlist(name):
        values.extend(item.strip() for item in raw.split(",") if item.strip())
    return tuple(values)


def _integer(request, name: str, default: int | None = None) -> int | None:
    raw = request.GET.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ExplorerValidationError(f"{name} must be an integer") from error


def _filters(request) -> GraphFilters:
    return GraphFilters(
        relation_types=_values(request, "relation"),
        document_kinds=_values(request, "kind"),
        departments=_values(request, "department"),
        validation_states=_values(request, "validation"),
        source_origins=_values(request, "source_origin"),
        year_min=_integer(request, "year_min"),
        year_max=_integer(request, "year_max"),
        connectivity=request.GET.get("connectivity", "all"),
        relationship_presence=request.GET.get("presence", "any"),
        component_id=request.GET.get("component", "").strip(),
        graph_scope=request.GET.get("scope", "administrative").strip(),
        max_nodes=_integer(request, "max_nodes", 500) or 500,
        max_edges=_integer(request, "max_edges", 2_000) or 2_000,
    )


def _error(error: Exception, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": str(error), "read_only": True}, status=status)


@require_GET
def api_summary(request):
    return JsonResponse(get_explorer_service().get_graph_summary())


@require_GET
def api_full_graph(request):
    try:
        payload = get_explorer_service().get_full_graph(_filters(request))
    except ExplorerValidationError as error:
        return _error(error)
    response = JsonResponse(payload)
    if request.GET.get("download") == "1":
        response["Content-Disposition"] = 'attachment; filename="knowledge-graph-filtered.json"'
    return response


@require_GET
def api_neighborhood(request, document_id: int):
    try:
        depth = _integer(request, "depth", 1) or 1
        payload = get_explorer_service().get_document_neighborhood(
            document_id, direction=request.GET.get("direction", "both"), depth=depth,
            filters=_filters(request),
        )
    except ExplorerValidationError as error:
        return _error(error)
    except KeyError:
        return _error(Http404("Document node not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_search(request):
    query = request.GET.get("q", "").strip()[:300]
    try:
        limit = _integer(request, "limit", 25) or 25
        payload = get_explorer_service().search_graph_nodes(query, filters=_filters(request), limit=limit)
    except ExplorerValidationError as error:
        return _error(error)
    return JsonResponse(payload)


@require_GET
def api_node_detail(request, node_id: str):
    try:
        payload = get_explorer_service().get_node_details(node_id)
    except ExplorerValidationError as error:
        return _error(error)
    if payload is None:
        return _error(Http404("Graph node not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_edge_detail(request, relation_id: int):
    try:
        payload = get_explorer_service().get_edge_details(relation_id)
    except ExplorerValidationError as error:
        return _error(error)
    if payload is None:
        return _error(Http404("Validated graph relation not found"), 404)
    return JsonResponse(payload)


@require_GET
def api_components(request):
    return JsonResponse(get_explorer_service().get_components())


def _visible_or_404(request, item_id):
    try:
        return get_visible_work_item(request.user, item_id)
    except WorkbenchError as exc:
        raise Http404(exc.safe_message) from exc


@login_required
@require_GET
def unresolved_list(request):
    queryset = visible_work_items(request.user).select_related("organization", "assigned_to", "claimed_by")
    summary = {
        "total": queryset.count(),
        "open": queryset.filter(status=WorkItemStatus.OPEN).count(),
        "claimed": queryset.filter(status=WorkItemStatus.CLAIMED).count(),
        "submitted": queryset.filter(status__in=(WorkItemStatus.DOCUMENT_SUBMITTED, WorkItemStatus.AWAITING_RESOLUTION)).count(),
        "resolved": queryset.filter(status__in=(WorkItemStatus.RESOLVED_AUTO, WorkItemStatus.RESOLVED_HUMAN)).count(),
    }
    selected = {
        "status": request.GET.get("status", ""),
        "type": request.GET.get("type", ""),
        "priority": request.GET.get("priority", ""),
        "local": request.GET.get("local", ""),
        "assignment": request.GET.get("assignment", ""),
        "q": request.GET.get("q", "").strip()[:200],
        "sort": request.GET.get("sort", "priority"),
    }
    if selected["status"] in WorkItemStatus.values:
        queryset = queryset.filter(status=selected["status"])
    if selected["type"] in WorkItemType.values:
        queryset = queryset.filter(work_type=selected["type"])
    if selected["priority"] in WorkItemPriority.values:
        queryset = queryset.filter(priority=selected["priority"])
    if selected["local"] in MatchStatus.values:
        queryset = queryset.filter(local_match_status=selected["local"])
    if selected["assignment"] == "mine":
        queryset = queryset.filter(Q(assigned_to=request.user) | Q(claimed_by=request.user))
    elif selected["assignment"] == "unassigned":
        queryset = queryset.filter(assigned_to__isnull=True, claimed_by__isnull=True)
    if selected["q"]:
        queryset = queryset.filter(
            Q(display_identifier__icontains=selected["q"])
            | Q(normalized_target_identity__icontains=selected["q"])
            | Q(target_department__icontains=selected["q"])
        )
    sort_options = {
        "updated": ("-updated_at", "id"),
        "mentions": ("-mention_count", "id"),
        "oldest": ("created_at", "id"),
    }
    if selected["sort"] == "priority":
        queryset = queryset.annotate(
            priority_rank=Case(
                When(priority=WorkItemPriority.HIGH, then=0),
                When(priority=WorkItemPriority.NORMAL, then=1),
                default=2,
                output_field=IntegerField(),
            )
        ).order_by("priority_rank", "-mention_count", "created_at")
    else:
        queryset = queryset.order_by(*sort_options.get(selected["sort"], sort_options["updated"]))
    page_obj = Paginator(queryset, 25).get_page(request.GET.get("page"))
    return render(request, "knowledge_graph/unresolved_workbench_list.html", {
        "page_obj": page_obj,
        "summary": summary,
        "filters": selected,
        "statuses": WorkItemStatus.choices,
        "work_types": WorkItemType.choices,
        "priorities": WorkItemPriority.choices,
        "match_statuses": MatchStatus.choices,
        "active_section": "unresolved",
        "context_mode": "unresolved",
    })


@login_required
@require_GET
def unresolved_detail(request, candidate_id: int):
    link = UnresolvedReferenceCandidateLink.objects.filter(
        candidate_id=candidate_id,
        work_item__organization_id__in=visible_work_items(request.user).values("organization_id"),
    ).first()
    if link is None:
        raise Http404("Unresolved target not found")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=link.work_item_id)


@login_required
@require_GET
def work_item_detail(request, item_id):
    item = _visible_or_404(request, item_id)
    try:
        evidence_page = max(1, int(request.GET.get("evidence_page", "1")))
    except ValueError:
        evidence_page = 1
    evidence = evidence_for_work_item(item, page=evidence_page)
    corpus_documents = document_summaries(item.corpus_candidate_ids)
    local_match = local_catalog_record(item.local_catalog_id) if item.local_catalog_id else None
    submissions = item.submission_links.select_related("submission", "created_by").all()[:10]
    proposals = item.resolution_proposals.select_related("proposed_by", "decided_by", "approval_case").all()[:10]
    pending = next((proposal for proposal in proposals if proposal.state == ProposalState.PENDING), None)
    can_approve = can_approve_review(request.user, item)
    active_claim = bool(item.claimed_by_id == request.user.pk and item.claim_expires_at and item.claim_expires_at > timezone.now())
    can_act = can_approve or item.assigned_to_id == request.user.pk or active_claim
    officers = []
    if can_approve:
        officers = [grant.user for grant in AccessGrant.objects.filter(
            organization_id=item.organization_id,
            role=AccessRole.OFFICER,
            state=AccessGrantState.ACTIVE,
            user__is_active=True,
        ).select_related("user").order_by("user__username")]
    decision_form = None
    if pending and pending.approval_case_id and can_approve and pending.approval_case.state == "PENDING":
        decision_form = ResolutionDecisionForm(initial={
            "expected_case_version": pending.approval_case.version,
            "expected_subject_version": pending.version,
        })
    return render(request, "knowledge_graph/unresolved_workbench_detail.html", {
        "item": item,
        "evidence": evidence,
        "corpus_documents": corpus_documents,
        "local_match": local_match,
        "submissions": submissions,
        "proposals": proposals,
        "pending_proposal": pending,
        "can_approve": can_approve,
        "can_act": can_act,
        "can_claim": item.status in ACTIVE_WORK_ITEM_STATUSES and (not item.claimed_by_id or active_claim),
        "claim_form": VersionedActionForm(initial={"expected_version": item.version}),
        "release_form": VersionedActionForm(initial={"expected_version": item.version}),
        "assignment_form": AssignmentForm(initial={"expected_version": item.version, "assignee_id": item.assigned_to_id}, officers=officers),
        "upload_form": WorkItemUploadForm(initial={"expected_version": item.version, "claimed_identifier": item.display_identifier}, user=request.user),
        "local_form": LocalCatalogSubmissionForm(initial={"expected_version": item.version, "catalog_id": item.local_catalog_id}),
        "proposal_form": ResolutionProposalForm(initial={"expected_version": item.version}, documents=corpus_documents),
        "decision_form": decision_form,
        "close_form": CloseInvalidForm(initial={"expected_version": item.version}),
        "activities": item.activities.select_related("actor").all()[:20],
        "active_section": "unresolved",
        "context_mode": "unresolved",
    })


def _action_error(request, exc):
    if isinstance(exc, (WorkbenchError, DocumentIntakeError)):
        message = exc.safe_message
    else:
        message = "The requested operation could not be completed safely. Verify the selection and try again."
    messages.error(request, message)


@login_required
@require_POST
def work_item_claim(request, item_id):
    form = VersionedActionForm(request.POST)
    if form.is_valid():
        try:
            claim_work_item(actor=request.user, item_id=item_id, expected_version=form.cleaned_data["expected_version"])
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "Work item claimed.")
    else:
        messages.error(request, "The claim request is stale or invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_release(request, item_id):
    form = VersionedActionForm(request.POST)
    if form.is_valid():
        try:
            release_work_item(actor=request.user, item_id=item_id, expected_version=form.cleaned_data["expected_version"])
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "Work item released.")
    else:
        messages.error(request, "The release request is stale or invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_assign(request, item_id):
    item = _visible_or_404(request, item_id)
    officers = [grant.user for grant in AccessGrant.objects.filter(
        organization_id=item.organization_id, role=AccessRole.OFFICER,
        state=AccessGrantState.ACTIVE, user__is_active=True,
    ).select_related("user")]
    form = AssignmentForm(request.POST, officers=officers)
    if form.is_valid():
        try:
            assign_work_item(actor=request.user, item_id=item_id, assignee_id=int(form.cleaned_data["assignee_id"]), expected_version=form.cleaned_data["expected_version"])
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "Work item assigned.")
    else:
        messages.error(request, "The assignment request is invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_upload(request, item_id):
    form = WorkItemUploadForm(request.POST, request.FILES, user=request.user)
    if form.is_valid():
        try:
            submit_work_item_upload(
                actor=request.user, item_id=item_id,
                expected_version=form.cleaned_data["expected_version"],
                upload=form.cleaned_data["document"],
                claimed_identifier=form.cleaned_data["claimed_identifier"],
                claimed_source_url=form.cleaned_data["claimed_source_url"],
                submission_reason=form.cleaned_data["submission_reason"],
                source_context=form.cleaned_data["source_context"],
                ias_source_attested=form.cleaned_data.get("ias_source_attested", False),
            )
        except (WorkbenchError, DocumentIntakeError) as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "The PDF entered the existing secure Stage 3 validation and approval workflow.")
    else:
        messages.error(request, "The document submission form is invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_submit_local(request, item_id):
    form = LocalCatalogSubmissionForm(request.POST)
    if form.is_valid():
        try:
            submit_local_catalog_document(
                actor=request.user, item_id=item_id,
                expected_version=form.cleaned_data["expected_version"],
                catalog_id=form.cleaned_data["catalog_id"],
                submission_reason=form.cleaned_data["submission_reason"],
                ias_source_attested=form.cleaned_data.get("ias_source_attested", False),
            )
        except (WorkbenchError, DocumentIntakeError, OSError, ValueError) as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "The selected local PDF entered the existing secure Stage 3 workflow.")
    else:
        messages.error(request, "The local-document submission request is invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_propose(request, item_id):
    item = _visible_or_404(request, item_id)
    documents = document_summaries(item.corpus_candidate_ids)
    form = ResolutionProposalForm(request.POST, documents=documents)
    if form.is_valid():
        try:
            propose_resolution(
                actor=request.user, item_id=item_id,
                expected_version=form.cleaned_data["expected_version"],
                target_document_id=int(form.cleaned_data["target_document_id"]),
                reason=form.cleaned_data["reason"],
            )
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "Resolution proposed for IAS approval. The graph is unchanged until approval.")
    else:
        messages.error(request, "The resolution proposal is invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_auto_resolve(request, item_id):
    form = VersionedActionForm(request.POST)
    if form.is_valid():
        try:
            resolve_work_item_automatically(
                actor=request.user,
                item_id=item_id,
                expected_version=form.cleaned_data["expected_version"],
            )
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "The authoritative deterministic resolver created the validated reference relation.")
    else:
        messages.error(request, "The automatic-resolution request is stale or invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_decide(request, item_id, proposal_id):
    item = _visible_or_404(request, item_id)
    if not can_approve_review(request.user, item):
        return HttpResponseForbidden("In-scope IAS authority is required.")
    proposal = item.resolution_proposals.filter(pk=proposal_id, state=ProposalState.PENDING).select_related("approval_case").first()
    if proposal is None or proposal.approval_case_id is None:
        raise Http404
    form = ResolutionDecisionForm(request.POST)
    if form.is_valid():
        try:
            decide_approval_case(
                request.user,
                proposal.approval_case_id,
                decision=form.cleaned_data["decision"],
                reason=form.cleaned_data["reason"],
                expected_case_version=form.cleaned_data["expected_case_version"],
                expected_subject_version=form.cleaned_data["expected_subject_version"],
                correlation_id=proposal.correlation_id,
            )
        except GovernanceApprovalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "The governed reference-resolution decision was recorded.")
    else:
        messages.error(request, "The governance decision form is invalid.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)


@login_required
@require_POST
def work_item_close_invalid(request, item_id):
    form = CloseInvalidForm(request.POST)
    if form.is_valid():
        try:
            close_invalid(actor=request.user, item_id=item_id, expected_version=form.cleaned_data["expected_version"], reason=form.cleaned_data["reason"])
        except WorkbenchError as exc:
            _action_error(request, exc)
        else:
            messages.success(request, "The invalid target was closed without deleting its evidence.")
    else:
        messages.error(request, "A bounded closure reason is required.")
    return redirect("knowledge_graph_ui:work_item_detail", item_id=item_id)
