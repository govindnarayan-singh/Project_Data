from __future__ import annotations

import re

from .models import ReferenceIdentity


_LEGAL_CANONICAL_QUERIES = (
    ("RIGHTS_OF_PERSONS_WITH_DISABILITIES", "Rights of Persons with Disabilities Act 2016"),
    ("MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS", "Maharashtra Zilla Parishads and Panchayat Samitis Act 1961"),
    ("पर्यावरण_संरक्षण", "Environment Protection Act 1986"),
    ("हवा_प्रदूषण_प्रतिबंध", "Air Prevention and Control of Pollution Act 1981"),
    ("URBAN_AREAS_PROTECTION_PRESERVATION_OF_TREES", "Maharashtra Urban Areas Protection and Preservation of Trees Act 1975"),
    ("पुनर्विकास_ACT_1971", "Maharashtra Slum Areas Improvement Clearance and Redevelopment Act 1971"),
)


def _public_legal_query(identity: ReferenceIdentity) -> str:
    for marker, title in _LEGAL_CANONICAL_QUERIES:
        if marker in identity.normalized_identifier.upper() or marker in identity.normalized_identifier:
            return title
    canonical = identity.normalized_identifier.replace("_", " ")
    canonical = re.sub(r"^\d+\s+", "", canonical)
    return canonical


def generate_queries(identity: ReferenceIdentity) -> list[str]:
    """Generate bounded, deterministic queries from public document identity only."""
    queries: list[str] = []
    if identity.reference_kind in {"ACT", "RULE"}:
        queries.append(_public_legal_query(identity))
    else:
        identifier = identity.normalized_identifier.strip() or identity.raw_identifier.strip()
        queries.append(identifier)
        if identity.reference_date:
            queries.append(f"{identifier} {identity.reference_date.isoformat()}")
        if identity.issuer:
            queries.append(f"{identifier} {identity.issuer}")
    result: list[str] = []
    for query in queries:
        normalized = " ".join(query.split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:3]


def assert_public_query(query: str) -> None:
    forbidden = ("\\", ":\\", "postgresql://", "sha256", "database_url", "gr_root", "candidate_id")
    lowered = query.casefold()
    if any(token in lowered for token in forbidden):
        raise ValueError("Discovery query contains non-public application data")
    if len(query) > 240:
        raise ValueError("Discovery query exceeds the minimal public-identity boundary")


__all__ = ["assert_public_query", "generate_queries"]
