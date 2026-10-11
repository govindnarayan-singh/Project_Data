import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from django.db import connection

from config.test_database_safety import assert_safe_test_database
from config.test_runner import ProductionSafeDiscoverRunner

if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append("testserver")


# pytest-django is intentionally not a project dependency.  Without an
# explicit runner, Django TestCase classes point at DATABASES["default"] and a
# TransactionTestCase can flush the live database.  Provision the normal
# Django test database for direct pytest invocations and tear it down when the
# session ends.  `manage.py test` does not load this pytest hook.
_database_runner = None
_database_config = None


def pytest_sessionstart(session):
    global _database_runner, _database_config
    effective_name = assert_safe_test_database(settings.DATABASES["default"])
    print(f"AUTOMATED_TEST_DATABASE[default]={effective_name}", flush=True)
    # Keep the isolated database between runs. Some PostgreSQL integration
    # tests intentionally open secondary connections; retaining test_grdb
    # avoids an unsafe/fragile DROP while those connections unwind.
    _database_runner = ProductionSafeDiscoverRunner(interactive=False, verbosity=0, keepdb=True)
    _database_runner.setup_test_environment()
    _database_config = _database_runner.setup_databases()
    assert_safe_test_database(
        {**connection.settings_dict, "TEST": {"NAME": connection.settings_dict["NAME"]}},
    )


def pytest_sessionfinish(session, exitstatus):
    if _database_runner is not None and _database_config is not None:
        _database_runner.teardown_databases(_database_config)
        _database_runner.teardown_test_environment()
