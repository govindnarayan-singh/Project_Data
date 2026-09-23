from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import SetPasswordForm
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import AccessRequestForm, DecisionForm, GrantActionForm, LoginForm, RequestStatusForm
from .models import AccessGrant, AccessRequest, AccessRequestState, OrganizationUnit
from .permissions import authorized_organization_ids, has_application_access, ias_required
from .services import (
    AccessDecisionError,
    AccessWorkflowError,
    activate_account,
    approve_access_request,
    change_officer_grant_state,
    clear_login_rate_limit,
    expire_stale_requests,
    login_rate_limit,
    lookup_request_status,
    reject_access_request,
    submit_access_request,
    valid_activation_token,
)


def _safe_next(request: HttpRequest, candidate: str) -> str:
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return candidate
    return reverse("search_ui:search")


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("search_ui:search" if has_application_access(request.user) else "identity_access:access_denied")
    initial_next = request.GET.get("next", "")
    form = LoginForm(request.POST or None, initial={"next": initial_next})
    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["identifier"].strip()
        allowed, source, account_source = login_rate_limit(request, identifier)
        user = authenticate(request, username=identifier, password=form.cleaned_data["password"]) if allowed else None
        if user is not None:
            clear_login_rate_limit(source, account_source)
            login(request, user)
            return redirect(_safe_next(request, form.cleaned_data.get("next", "")))
        form.add_error(None, "Unable to sign in with the supplied credentials.")
    return render(request, "identity_access/login.html", {"form": form})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("identity_access:login")


@require_http_methods(["GET", "POST"])
def request_access(request: HttpRequest) -> HttpResponse:
    form = AccessRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data.get("website"):
            return render(request, "identity_access/request_submitted.html", {"bot_submission": True})
        try:
            submitted = submit_access_request(form.cleaned_data, request)
        except AccessWorkflowError as exc:
            status = 429 if exc.code == "RATE_LIMITED" else 400
            return render(
                request,
                "identity_access/request_access.html",
                {"form": form, "submission_error": "The request could not be accepted. Contact your IAS authority if the issue persists."},
                status=status,
            )
        return render(
            request,
            "identity_access/request_submitted.html",
            {"access_request": submitted.request, "status_token": submitted.status_token},
        )
    return render(request, "identity_access/request_access.html", {"form": form})


@require_http_methods(["GET", "POST"])
def request_status(request: HttpRequest) -> HttpResponse:
    form = RequestStatusForm(request.POST or None)
    access_request = None
    lookup_failed = False
    if request.method == "POST" and form.is_valid():
        access_request = lookup_request_status(form.cleaned_data["request_id"], form.cleaned_data["status_token"])
        lookup_failed = access_request is None
    return render(
        request,
        "identity_access/request_status.html",
        {"form": form, "access_request": access_request, "lookup_failed": lookup_failed},
    )


@require_http_methods(["GET", "POST"])
def activate_account_view(request: HttpRequest, user_id: int, token: str) -> HttpResponse:
    token_record = valid_activation_token(user_id, token)
    if token_record is None:
        return render(request, "identity_access/activate.html", {"invalid": True}, status=400)
    form = SetPasswordForm(token_record.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = activate_account(user_id, token, form.cleaned_data["new_password1"])
        except AccessWorkflowError:
            return render(request, "identity_access/activate.html", {"invalid": True}, status=400)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("search_ui:search")
    return render(request, "identity_access/activate.html", {"form": form, "invalid": False})


@require_GET
def access_denied(request: HttpRequest) -> HttpResponse:
    return render(request, "identity_access/access_denied.html", status=403)


@require_GET
@ias_required
def ias_queue(request: HttpRequest) -> HttpResponse:
    expire_stale_requests()
    scope_ids = authorized_organization_ids(request.user)
    queryset = AccessRequest.objects.filter(organization_id__in=scope_ids).select_related("organization")
    search = request.GET.get("q", "").strip()[:100]
    if search:
        queryset = queryset.filter(
            Q(full_name__icontains=search)
            | Q(official_email__icontains=search)
            | Q(employee_identifier__icontains=search)
        )
    state = request.GET.get("state", AccessRequestState.PENDING_IAS_APPROVAL)
    valid_states = {value for value, _ in AccessRequestState.choices}
    if state in valid_states:
        queryset = queryset.filter(state=state)
    else:
        state = AccessRequestState.PENDING_IAS_APPROVAL
        queryset = queryset.filter(state=state)
    organization_id = request.GET.get("organization", "")
    if organization_id.isdigit() and int(organization_id) in scope_ids:
        queryset = queryset.filter(organization_id=int(organization_id))
    age = request.GET.get("age", "")
    if age in {"1", "7", "30"}:
        queryset = queryset.filter(created_at__lte=timezone.now() - timedelta(days=int(age)))
    page = Paginator(queryset.order_by("-created_at"), 25).get_page(request.GET.get("page"))
    organizations = OrganizationUnit.objects.filter(pk__in=scope_ids).order_by("name")
    return render(request, "identity_access/ias_queue.html", {
        "page_obj": page,
        "organizations": organizations,
        "states": AccessRequestState.choices,
        "filters": {"q": search, "state": state, "organization": organization_id, "age": age},
        "active_section": "access-queue",
    })


@require_GET
@ias_required
def ias_request_detail(request: HttpRequest, request_id) -> HttpResponse:
    scope_ids = authorized_organization_ids(request.user)
    access_request = get_object_or_404(
        AccessRequest.objects.select_related("organization", "decided_by").prefetch_related("transitions"),
        pk=request_id,
        organization_id__in=scope_ids,
    )
    form = DecisionForm(initial={"expected_version": access_request.version})
    return render(request, "identity_access/ias_request_detail.html", {
        "access_request": access_request,
        "decision_form": form,
        "active_section": "access-queue",
    })


@require_POST
@ias_required
def ias_approve_request(request: HttpRequest, request_id) -> HttpResponse:
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A valid current version and decision comment are required.")
        return redirect("identity_access:ias_request_detail", request_id=request_id)
    try:
        approved = approve_access_request(
            request.user,
            request_id,
            expected_version=form.cleaned_data["expected_version"],
            reason=form.cleaned_data["decision_reason"],
        )
    except (AccessDecisionError, AccessRequest.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, AccessDecisionError) else "Request not found.")
        return redirect("identity_access:ias_request_detail", request_id=request_id)
    activation_path = reverse("identity_access:activate", kwargs={"user_id": approved.user_id, "token": approved.activation_token})
    messages.success(request, "Approved. Copy the one-time activation link now; it is not stored in plaintext.")
    return render(request, "identity_access/activation_handoff.html", {
        "activation_url": request.build_absolute_uri(activation_path),
        "access_request": approved.request,
        "active_section": "access-queue",
    })


@require_POST
@ias_required
def ias_reject_request(request: HttpRequest, request_id) -> HttpResponse:
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A valid current version and decision reason are required.")
        return redirect("identity_access:ias_request_detail", request_id=request_id)
    try:
        reject_access_request(
            request.user,
            request_id,
            expected_version=form.cleaned_data["expected_version"],
            reason=form.cleaned_data["decision_reason"],
        )
    except (AccessDecisionError, AccessRequest.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, AccessDecisionError) else "Request not found.")
    else:
        messages.success(request, "The request was rejected.")
    return redirect("identity_access:ias_request_detail", request_id=request_id)


@require_GET
@ias_required
def member_list(request: HttpRequest) -> HttpResponse:
    scope_ids = authorized_organization_ids(request.user)
    grants = AccessGrant.objects.filter(organization_id__in=scope_ids, role="OFFICER").select_related("user", "organization").order_by("organization__name", "user__username")
    page = Paginator(grants, 25).get_page(request.GET.get("page"))
    return render(request, "identity_access/member_list.html", {"page_obj": page, "active_section": "access-members"})


@require_POST
@ias_required
def member_state_action(request: HttpRequest, grant_id: int) -> HttpResponse:
    form = GrantActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A valid state action, version, and reason are required.")
        return redirect("identity_access:member_list")
    try:
        change_officer_grant_state(
            request.user,
            grant_id,
            expected_version=form.cleaned_data["expected_version"],
            new_state=form.cleaned_data["new_state"],
            reason=form.cleaned_data["reason"],
        )
    except (AccessDecisionError, AccessGrant.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, AccessDecisionError) else "Access grant not found.")
    else:
        messages.success(request, "Officer access state updated.")
    return redirect("identity_access:member_list")
