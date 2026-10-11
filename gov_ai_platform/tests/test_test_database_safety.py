from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.test_database_safety import assert_safe_test_database, effective_test_database_name


POSTGRES_CONFIG = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "grdb",
}


def test_default_postgres_test_name_is_isolated():
    assert effective_test_database_name(POSTGRES_CONFIG) == "test_grdb"
    assert assert_safe_test_database(POSTGRES_CONFIG) == "test_grdb"


def test_explicit_isolated_test_database_is_allowed():
    config = {**POSTGRES_CONFIG, "TEST": {"NAME": "test_grdb"}}
    assert assert_safe_test_database(config) == "test_grdb"


def test_production_database_as_test_database_is_blocked():
    config = {**POSTGRES_CONFIG, "TEST": {"NAME": "grdb"}}
    with pytest.raises(ImproperlyConfigured, match="FATAL TEST DATABASE SAFETY GUARD"):
        assert_safe_test_database(config)
