from __future__ import annotations

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.identity_access.models import AccessRequest, AccessRequestState, AccessRequestTransition
from apps.identity_access.permissions import has_application_access
from apps.identity_access.services import AccessWorkflowError, _get_locked_access_request, normalize_email


class Command(BaseCommand):
    help = "Validate and optionally clear one incorrect authenticated-session requester link."

    def add_arguments(self, parser):
        parser.add_argument("--request-id", required=True)
        parser.add_argument("--expected-requester-user-id", required=True, type=int)
        parser.add_argument("--expected-email-sha256", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        expected_digest = options["expected_email_sha256"].strip().casefold()
        if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
            raise CommandError("--expected-email-sha256 must be a lowercase SHA-256 digest.")

        with transaction.atomic():
            try:
                access_request = _get_locked_access_request(options["request_id"])
            except (AccessRequest.DoesNotExist, ValueError) as exc:
                raise CommandError("Access request not found.") from exc
            if access_request.state != AccessRequestState.PENDING_IAS_APPROVAL:
                raise CommandError("Only a pending IAS approval request can be repaired.")
            if access_request.requester_user_id != options["expected_requester_user_id"]:
                raise CommandError("Requester user does not match the explicit expected user ID.")
            actual_digest = hashlib.sha256(access_request.normalized_email.encode("utf-8")).hexdigest()
            if actual_digest != expected_digest:
                raise CommandError("Request email digest does not match the explicit expected digest.")
            requester = access_request.requester_user
            if requester is None or not has_application_access(requester):
                raise CommandError("The linked requester is not a currently authorized user.")
            try:
                requester_email = normalize_email(requester.email)
            except AccessWorkflowError as exc:
                raise CommandError("The linked user's email is unavailable or invalid.") from exc
            if requester_email == access_request.normalized_email:
                raise CommandError("Refusing to clear a genuine self-request linkage.")

            old_version = access_request.version
            result = {
                "request_id": str(access_request.pk),
                "old_requester_user_id": access_request.requester_user_id,
                "new_requester_user_id": None,
                "old_version": old_version,
                "new_version": old_version + 1,
                "state": access_request.state,
                "applied": bool(options["apply"]),
            }
            if options["apply"]:
                access_request.requester_user = None
                access_request.version += 1
                access_request.save(update_fields=("requester_user", "version", "updated_at"))
                AccessRequestTransition.objects.create(
                    request=access_request,
                    old_state=access_request.state,
                    new_state=access_request.state,
                    reason="Stage 1.1 repair: cleared authenticated-session requester link after verified identity mismatch.",
                    version=access_request.version,
                )
            else:
                transaction.set_rollback(True)

        self.stdout.write(json.dumps(result, sort_keys=True))
