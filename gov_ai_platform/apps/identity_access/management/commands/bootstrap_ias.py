from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email, validate_slug
from django.db import transaction
from django.urls import reverse

from apps.governance.audit import new_correlation_id, record_governance_event
from apps.governance.constants import AuditAction, AuditCategory, AuditOutcome
from apps.governance.exceptions import AuditError
from apps.identity_access.models import AccessGrant, AccessGrantState, AccessRole, IASAuthorityScope, OrganizationUnit
from apps.identity_access.services import create_activation_token


class Command(BaseCommand):
    help = "Explicitly bootstrap one scoped IAS identity with no password and a one-time activation link."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--organization-code", required=True)
        parser.add_argument("--organization-name", required=True)
        parser.add_argument("--base-url", default="http://127.0.0.1:8000")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"].strip()
        email = options["email"].strip().casefold()
        code = options["organization_code"].strip().casefold()
        name = options["organization_name"].strip()
        try:
            validate_email(email)
            validate_slug(code)
        except ValidationError as exc:
            raise CommandError("A valid official email and slug-style organization code are required.") from exc
        if not username or len(username) > User._meta.get_field(User.USERNAME_FIELD).max_length:
            raise CommandError("Username is empty or exceeds the configured user-model limit.")
        if not name or len(name) > 150:
            raise CommandError("Organization name is empty or too long.")

        operation_id = new_correlation_id()
        try:
            with transaction.atomic():
                if User.objects.filter(username__iexact=username).exists() or User.objects.filter(email__iexact=email).exists():
                    raise CommandError("Refusing duplicate username or email; no privileged identity was created.")
                organization, created = OrganizationUnit.objects.get_or_create(code=code, defaults={"name": name})
                if not created and organization.name != name:
                    raise CommandError("Organization code already exists with a different name.")
                if not organization.is_active:
                    raise CommandError("Organization is inactive.")
                user = User(username=username, email=email, is_active=True, is_staff=False, is_superuser=False)
                user.set_unusable_password()
                user.save()
                grant = AccessGrant.objects.create(
                    user=user,
                    organization=organization,
                    role=AccessRole.IAS_OFFICER,
                    state=AccessGrantState.ACTIVE,
                )
                IASAuthorityScope.objects.create(user=user, organization=organization, is_active=True)
                token = create_activation_token(user)
                record_governance_event(
                    category=AuditCategory.IDENTITY_ACCESS,
                    action=AuditAction.IAS_BOOTSTRAPPED,
                    outcome=AuditOutcome.SUCCESS,
                    organization_id=organization.pk,
                    resource_type="auth.User",
                    resource_id=str(user.pk),
                    correlation_id=operation_id,
                    reason_code="OPERATOR_BOOTSTRAP",
                    new_state="ACTIVE",
                    metadata={"grant_id": grant.pk, "grant_role": grant.role, "grant_state": grant.state},
                )
        except AuditError as exc:
            raise CommandError("IAS bootstrap was rolled back because governance audit recording failed.") from exc

        path = reverse("identity_access:activate", kwargs={"user_id": user.pk, "token": token})
        activation_url = f"{options['base_url'].rstrip('/')}{path}"
        self.stdout.write(self.style.SUCCESS("IAS identity created with an unusable password and scoped active grant."))
        self.stdout.write("Copy this one-time activation URL through an authorized secure channel; it is not stored in plaintext:")
        self.stdout.write(activation_url)
