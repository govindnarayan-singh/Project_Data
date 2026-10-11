from __future__ import annotations

from django.db import connections
from django.test.runner import DiscoverRunner

from .test_database_safety import assert_safe_test_database


class ProductionSafeDiscoverRunner(DiscoverRunner):
    """Refuse to initialize Django tests when an alias resolves to production."""

    def setup_databases(self, **kwargs):
        for alias in connections:
            effective_name = assert_safe_test_database(
                connections[alias].settings_dict,
                alias=alias,
            )
            print(f"AUTOMATED_TEST_DATABASE[{alias}]={effective_name}", flush=True)
        return super().setup_databases(**kwargs)
