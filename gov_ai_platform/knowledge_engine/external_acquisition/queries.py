from __future__ import annotations

from .models import ReferenceIdentity


def generate_queries(identity: ReferenceIdentity) -> list[str]:
    """Generate bounded deterministic discovery queries without an LLM."""
    queries: list[str] = []
    identifier = identity.raw_identifier.strip() or identity.normalized_identifier
    if identity.reference_kind in {"ACT", "RULE"}:
        queries.append(identifier.replace("_", " "))
    else:
        queries.append(identifier)
        if identity.issuer:
            queries.append(f"{identifier} {identity.issuer}")
        if identity.reference_date:
            queries.append(f"{identifier} {identity.reference_date.isoformat()}")
    result: list[str] = []
    for query in queries:
        normalized = " ".join(query.split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:3]


__all__ = ["generate_queries"]
