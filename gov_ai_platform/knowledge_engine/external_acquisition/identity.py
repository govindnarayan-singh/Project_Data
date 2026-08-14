from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from knowledge_engine.graph.normalization import normalize_document_number, normalize_legal_reference

from .models import ReferenceIdentity


@dataclass(frozen=True, slots=True)
class IdentityMatch:
    status: str
    discovered_identifier: str
    evidence: dict[str, str | bool]


def verify_identity(
    reference: ReferenceIdentity,
    *,
    discovered_title: str,
    discovered_identifier: str = "",
    discovered_date: date | None = None,
    discovered_issuer: str = "",
) -> IdentityMatch:
    if reference.reference_kind in {"ACT", "RULE"}:
        normalized = normalize_legal_reference(discovered_title, reference.reference_kind)
    else:
        normalized = normalize_document_number(discovered_identifier)
    number_match = bool(normalized and normalized == reference.normalized_identifier)
    date_match = not reference.reference_date or not discovered_date or reference.reference_date == discovered_date
    issuer_match = not reference.issuer or not discovered_issuer or reference.issuer.casefold() == discovered_issuer.casefold()
    evidence = {
        "expected_identifier": reference.normalized_identifier,
        "discovered_identifier": normalized,
        "number_match": number_match,
        "date_match": date_match,
        "issuer_match": issuer_match,
    }
    if number_match and date_match and issuer_match:
        status = "MATCH_EXACT" if (reference.reference_date is None or discovered_date == reference.reference_date) else "MATCH_STRONG"
    elif number_match and date_match:
        status = "MATCH_STRONG"
    elif number_match:
        status = "AMBIGUOUS"
    else:
        status = "MISMATCH"
    return IdentityMatch(status, normalized, evidence)


__all__ = ["IdentityMatch", "verify_identity"]
