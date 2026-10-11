from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ImproperlyConfigured


PRODUCTION_DATABASE_NAMES = frozenset({"grdb"})


def effective_test_database_name(database_config: Mapping[str, Any]) -> str:
    test_config = database_config.get("TEST") or {}
    explicit_name = str(test_config.get("NAME") or "").strip()
    if explicit_name:
        return explicit_name

    engine = str(database_config.get("ENGINE") or "")
    configured_name = str(database_config.get("NAME") or "").strip()
    if engine.endswith("sqlite3"):
        return ":memory:"
    return f"test_{configured_name}" if configured_name else ""


def assert_safe_test_database(database_config: Mapping[str, Any], *, alias: str = "default") -> str:
    effective_name = effective_test_database_name(database_config)
    normalized_name = effective_name.casefold()
    if not effective_name or normalized_name in PRODUCTION_DATABASE_NAMES:
        raise ImproperlyConfigured(
            f"FATAL TEST DATABASE SAFETY GUARD: alias {alias!r} resolves to "
            f"unsafe database {effective_name or '<empty>'!r}. Automated tests "
            "must use test_grdb or another explicitly disposable database."
        )
    return effective_name
