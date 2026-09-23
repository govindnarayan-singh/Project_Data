from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.identity_access.models import AccessGrant, AccessRole, OrganizationUnit


class AuthorizedViewTestCase(TestCase):
    """Exercise legacy view tests through the real Stage 1 access boundary."""

    def setUp(self):
        super().setUp()
        organization = OrganizationUnit.objects.create(code="test-unit", name="Test Unit")
        user = get_user_model().objects.create_user(
            username="authorized-view-tester",
            email="authorized-view-tester@example.gov",
            password="TestOnlyPass-9402",
        )
        AccessGrant.objects.create(user=user, organization=organization, role=AccessRole.OFFICER)
        self.client.force_login(user)
