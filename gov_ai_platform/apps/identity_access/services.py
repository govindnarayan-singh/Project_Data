from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AccessGrant,
    AccessGrantState,
    AccessRequest,
    AccessRequestState,
    AccessRequestTransition,
    AccessRole,
    ActivationToken,
    PENDING_REQUEST_STATES,
    RateLimitBucket,
    ValidationOutcome,
)
from .permissions import can_manage_organization


class AccessWorkflowError(Exception):
    def __init__(self, code: str, message: str = "The request could not be accepted."):
        super().__init__(message)
        self.code = code


class AccessDecisionError(AccessWorkflowError):
    pass


@dataclass(frozen=True)
class SubmittedAccessRequest:
    request: AccessRequest
    status_token: str


@dataclass(frozen=True)
class ApprovedAccessRequest:
    request: AccessRequest
    activation_token: str
    user_id: int


def _digest(namespace: str, value: str) -> str:
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, f"{namespace}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_email(value: str) -> str:
    local, separator, domain = value.strip().casefold().rpartition("@")
    if not separator or not local or not domain:
        raise AccessWorkflowError(ValidationOutcome.REJECT_INVALID)
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise AccessWorkflowError(ValidationOutcome.REJECT_INVALID) from exc
    alias_separator = settings.ACCESS_EMAIL_ALIAS_SEPARATOR
    if alias_separator and domain in settings.ACCESS_ALIAS_NORMALIZATION_DOMAINS:
        local = local.split(alias_separator, 1)[0]
    return f"{local}@{domain}"


def normalize_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[^\w]", "", normalized, flags=re.UNICODE)
    if not normalized:
        raise AccessWorkflowError(ValidationOutcome.REJECT_INVALID)
    return normalized[:64]


def client_source(request) -> str:
    remote_text = (request.META.get("REMOTE_ADDR") or "0.0.0.0").strip()
    try:
        remote = ipaddress.ip_address(remote_text)
    except ValueError:
        return "0.0.0.0"

    networks = []
    for configured in settings.ACCESS_TRUSTED_PROXY_NETWORKS:
        try:
            networks.append(ipaddress.ip_network(configured, strict=False))
        except ValueError:
            continue
    if not any(remote in network for network in networks):
        return remote.compressed

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    try:
        chain = [ipaddress.ip_address(part.strip()) for part in forwarded.split(",") if part.strip()]
    except ValueError:
        return remote.compressed
    chain.append(remote)
    for address in reversed(chain):
        if not any(address in network for network in networks):
            return address.compressed
    return remote.compressed


def consume_rate_limit(namespace: str, raw_key: str, *, limit: int, window_seconds: int, block_seconds: int | None = None) -> bool:
    now = timezone.now()
    block_seconds = block_seconds if block_seconds is not None else settings.ACCESS_THROTTLE_BLOCK_SECONDS
    key_hash = _digest("throttle", raw_key)
    RateLimitBucket.objects.filter(expires_at__lt=now).delete()
    with transaction.atomic():
        bucket, created = RateLimitBucket.objects.get_or_create(
            namespace=namespace,
            key_hash=key_hash,
            defaults={
                "window_started_at": now,
                "expires_at": now + timedelta(seconds=window_seconds + block_seconds),
                "attempts": 0,
            },
        )
        if not created:
            bucket = RateLimitBucket.objects.select_for_update().get(pk=bucket.pk)
        if not created and bucket.window_started_at + timedelta(seconds=window_seconds) <= now:
            bucket.window_started_at = now
            bucket.expires_at = now + timedelta(seconds=window_seconds + block_seconds)
            bucket.blocked_until = None
            bucket.attempts = 0

        if bucket.blocked_until and bucket.blocked_until > now:
            return False
        bucket.attempts += 1
        allowed = bucket.attempts <= limit
        if not allowed:
            bucket.blocked_until = now + timedelta(seconds=block_seconds)
            bucket.expires_at = max(bucket.expires_at, bucket.blocked_until)
        bucket.save(update_fields=("window_started_at", "expires_at", "blocked_until", "attempts"))
        return allowed


def clear_rate_limit(namespace: str, raw_key: str) -> None:
    RateLimitBucket.objects.filter(namespace=namespace, key_hash=_digest("throttle", raw_key)).delete()


def login_rate_limit(request, identifier: str) -> tuple[bool, str, str]:
    source = client_source(request)
    account_source = f"{source}|{identifier.strip().casefold()}"
    source_ok = consume_rate_limit(
        "login-source",
        source,
        limit=settings.ACCESS_LOGIN_SOURCE_LIMIT,
        window_seconds=settings.ACCESS_LOGIN_WINDOW_SECONDS,
    )
    combined_ok = consume_rate_limit(
        "login-account-source",
        account_source,
        limit=settings.ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT,
        window_seconds=settings.ACCESS_LOGIN_WINDOW_SECONDS,
    )
    return source_ok and combined_ok, source, account_source


def clear_login_rate_limit(source: str, account_source: str) -> None:
    clear_rate_limit("login-source", source)
    clear_rate_limit("login-account-source", account_source)


def _transition_request(access_request: AccessRequest, new_state: str, *, actor=None, reason: str = "") -> None:
    legal = {
        AccessRequestState.REQUESTED: {AccessRequestState.PENDING_VALIDATION, AccessRequestState.CANCELLED},
        AccessRequestState.PENDING_VALIDATION: {AccessRequestState.PENDING_IAS_APPROVAL, AccessRequestState.REJECTED},
        AccessRequestState.PENDING_IAS_APPROVAL: {
            AccessRequestState.APPROVED,
            AccessRequestState.REJECTED,
            AccessRequestState.EXPIRED,
            AccessRequestState.CANCELLED,
        },
    }
    if new_state not in legal.get(access_request.state, set()):
        raise AccessDecisionError("ILLEGAL_TRANSITION", "That state transition is not permitted.")
    old_state = access_request.state
    access_request.state = new_state
    access_request.version += 1
    access_request.save(update_fields=("state", "version", "updated_at"))
    AccessRequestTransition.objects.create(
        request=access_request,
        old_state=old_state,
        new_state=new_state,
        actor=actor,
        reason=reason[:1000],
        version=access_request.version,
    )


def submit_access_request(cleaned_data: dict, request) -> SubmittedAccessRequest:
    organization = cleaned_data["organization"]
    normalized_email = normalize_email(cleaned_data["official_email"])
    normalized_identity = normalize_identity(cleaned_data["employee_identifier"])
    source = client_source(request)
    window = settings.ACCESS_REQUEST_WINDOW_SECONDS
    checks = (
        consume_rate_limit("request-source", source, limit=settings.ACCESS_REQUEST_SOURCE_LIMIT, window_seconds=window),
        consume_rate_limit("request-email", normalized_email, limit=settings.ACCESS_REQUEST_EMAIL_LIMIT, window_seconds=window),
        consume_rate_limit("request-identity", normalized_identity, limit=settings.ACCESS_REQUEST_IDENTITY_LIMIT, window_seconds=window),
    )
    if not all(checks):
        raise AccessWorkflowError(ValidationOutcome.RATE_LIMITED)
    if not organization.is_active:
        raise AccessWorkflowError(ValidationOutcome.REJECT_INVALID)
    allowed_codes = settings.ACCESS_ALLOWED_ORGANIZATION_CODES
    if allowed_codes and organization.code.casefold() not in allowed_codes:
        raise AccessWorkflowError(ValidationOutcome.REJECT_INVALID)

    now = timezone.now()
    expire_stale_requests()
    pending = Q(state__in=PENDING_REQUEST_STATES) & (
        Q(normalized_email=normalized_email) | Q(normalized_identity=normalized_identity)
    )
    if AccessRequest.objects.filter(pending).exists():
        raise AccessWorkflowError(ValidationOutcome.REJECT_DUPLICATE)
    if AccessGrant.objects.filter(
        user__email__iexact=normalized_email,
        state=AccessGrantState.ACTIVE,
        starts_at__lte=now,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).exists():
        raise AccessWorkflowError(ValidationOutcome.REJECT_DUPLICATE)
    cooldown_after = now - timedelta(hours=settings.ACCESS_REJECTION_COOLDOWN_HOURS)
    if AccessRequest.objects.filter(
        Q(normalized_email=normalized_email) | Q(normalized_identity=normalized_identity),
        state=AccessRequestState.REJECTED,
        decided_at__gte=cooldown_after,
    ).exists():
        raise AccessWorkflowError(ValidationOutcome.COOLDOWN)

    email_domain = normalized_email.rsplit("@", 1)[1]
    risk_flags: list[str] = []
    outcome = ValidationOutcome.ALLOW_TO_QUEUE
    if settings.ACCESS_ALLOWED_EMAIL_DOMAINS and email_domain not in settings.ACCESS_ALLOWED_EMAIL_DOMAINS:
        risk_flags.append("EMAIL_DOMAIN_REQUIRES_MANUAL_REVIEW")
        outcome = ValidationOutcome.MANUAL_REVIEW_REQUIRED

    status_token = secrets.token_urlsafe(32)
    try:
        with transaction.atomic():
            access_request = AccessRequest.objects.create(
                full_name=cleaned_data["full_name"].strip(),
                official_email=cleaned_data["official_email"].strip(),
                normalized_email=normalized_email,
                employee_identifier=cleaned_data["employee_identifier"].strip(),
                normalized_identity=normalized_identity,
                organization=organization,
                designation=cleaned_data.get("designation", "").strip(),
                reason=cleaned_data["reason"].strip(),
                validation_outcome=outcome,
                risk_flags=risk_flags,
                source_fingerprint=_digest("source", source),
                status_token_digest=_digest("status", status_token),
                requester_user=request.user if getattr(request.user, "is_authenticated", False) else None,
                expires_at=now + timedelta(days=settings.ACCESS_REQUEST_EXPIRY_DAYS),
            )
            AccessRequestTransition.objects.create(
                request=access_request,
                old_state="",
                new_state=AccessRequestState.REQUESTED,
                actor=access_request.requester_user,
                reason="Public access request received",
                version=1,
            )
            _transition_request(access_request, AccessRequestState.PENDING_VALIDATION, reason="Server-side validation started")
            _transition_request(access_request, AccessRequestState.PENDING_IAS_APPROVAL, reason=outcome)
    except IntegrityError as exc:
        raise AccessWorkflowError(ValidationOutcome.REJECT_DUPLICATE) from exc
    return SubmittedAccessRequest(access_request, status_token)


def lookup_request_status(request_id, token: str) -> AccessRequest | None:
    _expire_request_if_stale(request_id)
    access_request = AccessRequest.objects.filter(pk=request_id).first()
    if access_request is None:
        return None
    supplied = _digest("status", token)
    if not secrets.compare_digest(access_request.status_token_digest, supplied):
        return None
    return access_request


def expire_stale_requests(*, batch_size: int = 200) -> int:
    now = timezone.now()
    expired = 0
    ids = list(
        AccessRequest.objects.filter(state=AccessRequestState.PENDING_IAS_APPROVAL, expires_at__lte=now)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    for request_id in ids:
        with transaction.atomic():
            access_request = AccessRequest.objects.select_for_update().get(pk=request_id)
            if access_request.state == AccessRequestState.PENDING_IAS_APPROVAL and access_request.expires_at <= now:
                _transition_request(access_request, AccessRequestState.EXPIRED, reason="Request expired before decision")
                expired += 1
    return expired


def _expire_request_if_stale(request_id) -> bool:
    with transaction.atomic():
        access_request = AccessRequest.objects.select_for_update().filter(pk=request_id).first()
        if (
            access_request
            and access_request.state == AccessRequestState.PENDING_IAS_APPROVAL
            and access_request.expires_at <= timezone.now()
        ):
            _transition_request(access_request, AccessRequestState.EXPIRED, reason="Request expired before decision")
            return True
    return False


def _new_activation_token(user, *, actor=None, access_request=None) -> str:
    now = timezone.now()
    ActivationToken.objects.filter(user=user, purpose=ActivationToken.PURPOSE_ACCOUNT_ACTIVATION, used_at__isnull=True).update(used_at=now)
    raw_token = secrets.token_urlsafe(32)
    ActivationToken.objects.create(
        user=user,
        access_request=access_request,
        token_digest=_digest("activation", raw_token),
        expires_at=now + timedelta(hours=settings.ACCESS_ACTIVATION_TOKEN_HOURS),
        created_by=actor,
    )
    return raw_token


def create_activation_token(user, *, actor=None, access_request=None) -> str:
    with transaction.atomic():
        return _new_activation_token(user, actor=actor, access_request=access_request)


def approve_access_request(actor, request_id, *, expected_version: int, reason: str) -> ApprovedAccessRequest:
    User = get_user_model()
    if _expire_request_if_stale(request_id):
        raise AccessDecisionError("EXPIRED", "The request has expired.")
    with transaction.atomic():
        access_request = AccessRequest.objects.select_for_update().select_related("organization", "requester_user").get(pk=request_id)
        if access_request.version != expected_version:
            raise AccessDecisionError("STALE_VERSION", "The request changed; refresh before deciding.")
        if access_request.state != AccessRequestState.PENDING_IAS_APPROVAL:
            raise AccessDecisionError("NOT_PENDING", "Only pending requests can be approved.")
        if access_request.expires_at <= timezone.now():
            raise AccessDecisionError("EXPIRED", "The request has expired.")
        if access_request.requester_user_id == actor.pk:
            raise AccessDecisionError("SELF_APPROVAL", "Self-approval is prohibited.")
        if not can_manage_organization(actor, access_request.organization_id):
            raise AccessDecisionError("OUT_OF_SCOPE", "The request is outside your organizational scope.")

        matching_users = list(User.objects.filter(email__iexact=access_request.normalized_email)[:2])
        if len(matching_users) > 1:
            raise AccessDecisionError("IDENTITY_COLLISION", "The identity requires operator resolution.")
        user = matching_users[0] if matching_users else None
        if user and AccessGrant.objects.filter(user=user, role=AccessRole.IAS_OFFICER).exists():
            raise AccessDecisionError("PRIVILEGED_IDENTITY", "A privileged identity cannot be repurposed.")
        if user is None:
            username = access_request.normalized_email[:150]
            if User.objects.filter(username__iexact=username).exists():
                username = f"officer-{hashlib.sha256(access_request.normalized_email.encode()).hexdigest()[:16]}"
            user = User(username=username, email=access_request.normalized_email, is_active=True)
            user.set_unusable_password()
            user.save()
        elif not user.is_active:
            user.is_active = True
            user.set_unusable_password()
            user.save(update_fields=("is_active", "password"))

        grant, created = AccessGrant.objects.get_or_create(
            user=user,
            organization=access_request.organization,
            role=AccessRole.OFFICER,
            defaults={"state": AccessGrantState.ACTIVE, "approved_by": actor},
        )
        if not created:
            grant.state = AccessGrantState.ACTIVE
            grant.approved_by = actor
            grant.starts_at = timezone.now()
            grant.expires_at = None
            grant.status_reason = ""
            grant.status_changed_by = actor
            grant.status_changed_at = timezone.now()
            grant.version += 1
            grant.save()

        access_request.decision_reason = reason.strip()[:1000]
        access_request.decided_by = actor
        access_request.decided_at = timezone.now()
        access_request.save(update_fields=("decision_reason", "decided_by", "decided_at", "updated_at"))
        _transition_request(access_request, AccessRequestState.APPROVED, actor=actor, reason=reason)
        raw_token = _new_activation_token(user, actor=actor, access_request=access_request)
        return ApprovedAccessRequest(access_request, raw_token, user.pk)


def reject_access_request(actor, request_id, *, expected_version: int, reason: str) -> AccessRequest:
    if _expire_request_if_stale(request_id):
        raise AccessDecisionError("EXPIRED", "The request has expired.")
    with transaction.atomic():
        access_request = AccessRequest.objects.select_for_update().get(pk=request_id)
        if access_request.version != expected_version:
            raise AccessDecisionError("STALE_VERSION", "The request changed; refresh before deciding.")
        if access_request.state != AccessRequestState.PENDING_IAS_APPROVAL:
            raise AccessDecisionError("NOT_PENDING", "Only pending requests can be rejected.")
        if access_request.requester_user_id == actor.pk:
            raise AccessDecisionError("SELF_APPROVAL", "Self-decision is prohibited.")
        if not can_manage_organization(actor, access_request.organization_id):
            raise AccessDecisionError("OUT_OF_SCOPE", "The request is outside your organizational scope.")
        access_request.decision_reason = reason.strip()[:1000]
        access_request.decided_by = actor
        access_request.decided_at = timezone.now()
        access_request.save(update_fields=("decision_reason", "decided_by", "decided_at", "updated_at"))
        _transition_request(access_request, AccessRequestState.REJECTED, actor=actor, reason=reason)
        return access_request


def valid_activation_token(user_id: int, raw_token: str) -> ActivationToken | None:
    return ActivationToken.objects.select_related("user").filter(
        user_id=user_id,
        token_digest=_digest("activation", raw_token),
        purpose=ActivationToken.PURPOSE_ACCOUNT_ACTIVATION,
        used_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()


def activate_account(user_id: int, raw_token: str, password: str):
    with transaction.atomic():
        token = ActivationToken.objects.select_for_update().select_related("user").filter(
            user_id=user_id,
            token_digest=_digest("activation", raw_token),
            purpose=ActivationToken.PURPOSE_ACCOUNT_ACTIVATION,
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()
        if token is None:
            raise AccessWorkflowError("INVALID_ACTIVATION", "This activation link is invalid or expired.")
        if not AccessGrant.objects.filter(user=token.user, state=AccessGrantState.ACTIVE).exists():
            raise AccessWorkflowError("INACTIVE_GRANT", "Access is no longer active.")
        token.user.set_password(password)
        token.user.save(update_fields=("password",))
        token.used_at = timezone.now()
        token.save(update_fields=("used_at",))
        return token.user


def change_officer_grant_state(actor, grant_id: int, *, expected_version: int, new_state: str, reason: str) -> AccessGrant:
    if new_state not in {AccessGrantState.SUSPENDED, AccessGrantState.REVOKED}:
        raise AccessDecisionError("INVALID_GRANT_STATE", "Only suspension or revocation is permitted here.")
    with transaction.atomic():
        grant = AccessGrant.objects.select_for_update().get(pk=grant_id)
        if grant.version != expected_version:
            raise AccessDecisionError("STALE_VERSION", "The access grant changed; refresh before deciding.")
        if grant.role != AccessRole.OFFICER:
            raise AccessDecisionError("ROLE_ESCALATION", "IAS grants cannot be changed through this workflow.")
        if not can_manage_organization(actor, grant.organization_id):
            raise AccessDecisionError("OUT_OF_SCOPE", "The access grant is outside your organizational scope.")
        if grant.state == new_state:
            return grant
        allowed = {
            AccessGrantState.ACTIVE: {AccessGrantState.SUSPENDED, AccessGrantState.REVOKED},
            AccessGrantState.SUSPENDED: {AccessGrantState.REVOKED},
        }
        if new_state not in allowed.get(grant.state, set()):
            raise AccessDecisionError("ILLEGAL_TRANSITION", "That grant transition is not permitted.")
        grant.state = new_state
        grant.status_reason = reason.strip()[:1000]
        grant.status_changed_by = actor
        grant.status_changed_at = timezone.now()
        grant.version += 1
        grant.save(update_fields=("state", "status_reason", "status_changed_by", "status_changed_at", "version", "updated_at"))
        return grant
