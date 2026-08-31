from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from knowledge_engine.graph.normalization import (
    normalize_document_number,
    normalize_legal_reference,
)

from .models import ReferenceIdentity


_GENERIC_LEGAL_TOKENS = {
    "ACT", "RULE", "RULES", "THE", "THIS", "OF", "BY", "INS", "SUBS",
    "MAHARASHTRA", "AMENDMENT", "PROVISIONS", "WHETHER", "DATE", "NOTE",
}


def legal_identity_quality(identifier: str) -> tuple[bool, str]:
    """Reject prose/footnote fragments before they become legal-title queries."""
    canonical = identifier.strip().upper()
    if not re.search(r"_(ACT|RULES?)_(19|20)\d{2}$", canonical):
        return False, "LEGAL_TITLE_AND_YEAR_REQUIRED"
    tokens = [token for token in canonical.split("_") if token]
    if tokens and tokens[0].isdigit():
        return False, "FOOTNOTE_PREFIX"
    substantive = [token for token in tokens[:-2] if token not in _GENERIC_LEGAL_TOKENS and not token.isdigit()]
    if not substantive:
        return False, "GENERIC_OR_PROSE_FRAGMENT"
    return True, "STRUCTURED_LEGAL_TITLE_YEAR"


def legal_version_semantics(identifier: str) -> str:
    canonical = identifier.upper()
    if "_AMENDMENT_ACT_" in canonical:
        return "AMENDMENT_ACT_EXACT"
    if "CONSOLIDATED" in canonical or "AS_AMENDED" in canonical:
        return "CONSOLIDATED_VERSION_OF_TARGET"
    if re.search(r"_ACT_(19|20)\d{2}$", canonical):
        return "PRINCIPAL_ACT_EXACT"
    return "AMBIGUOUS_VERSION"


def _legal_suffix_match(expected: str, discovered: str) -> bool:
    valid, _ = legal_identity_quality(expected)
    if not valid or not discovered.endswith(expected):
        return False
    expected_year = re.search(r"_(19|20)\d{2}$", expected)
    discovered_year = re.search(r"_(19|20)\d{2}$", discovered)
    return bool(expected_year and discovered_year and expected_year.group(0) == discovered_year.group(0))


@dataclass(frozen=True, slots=True)
class IdentityMatch:
    status: str
    discovered_identifier: str
    evidence: dict[str, str | bool]


def expected_identifier(reference: ReferenceIdentity) -> str:
    if reference.reference_kind in {"ACT", "RULE"}:
        # V5/V6 corrected identities already carry the complete title and
        # version semantics.  Preserve those before consulting legacy repair
        # aliases, which are only for damaged historical parser output.
        valid, _reason = legal_identity_quality(reference.normalized_identifier)
        damaged_prefixes = ("ACCORDING_TO_", "AS_PER_", "UNDER_", "SECTION_", "THE_SCHEDULE_")
        if valid and not reference.normalized_identifier.upper().startswith(damaged_prefixes):
            return reference.normalized_identifier
        marker_map = (
            ("RIGHTS_OF_PERSONS_WITH_DISABILITIES", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016"),
            ("MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS", "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_ACT_1961"),
            ("पर्यावरण_संरक्षण", "ENVIRONMENT_PROTECTION_ACT_1986"),
            ("हवा_प्रदूषण_प्रतिबंध", "AIR_PREVENTION_AND_CONTROL_OF_POLLUTION_ACT_1981"),
            ("URBAN_AREAS_PROTECTION_PRESERVATION_OF_TREES", "MAHARASHTRA_URBAN_AREAS_PROTECTION_AND_PRESERVATION_OF_TREES_ACT_1975"),
            ("पुनर्विकास_ACT_1971", "MAHARASHTRA_SLUM_AREAS_IMPROVEMENT_CLEARANCE_AND_REDEVELOPMENT_ACT_1971"),
        )
        for marker, canonical in marker_map:
            if marker in reference.normalized_identifier.upper() or marker in reference.normalized_identifier:
                return canonical
        normalized_raw = normalize_legal_reference(reference.raw_identifier, reference.reference_kind)
        return normalized_raw or reference.normalized_identifier
    return reference.normalized_identifier


def verify_identity(
    reference: ReferenceIdentity,
    *,
    discovered_title: str,
    discovered_identifier: str = "",
    discovered_date: date | None = None,
    discovered_issuer: str = "",
) -> IdentityMatch:
    expected = expected_identifier(reference)
    if reference.reference_kind in {"ACT", "RULE"}:
        normalized = normalize_legal_reference(discovered_title, reference.reference_kind)
    else:
        normalized = normalize_document_number(discovered_identifier)
    exact_number_match = bool(normalized and normalized == expected)
    legal_suffix_match = bool(
        reference.reference_kind in {"ACT", "RULE"}
        and normalized and _legal_suffix_match(expected, normalized)
    )
    administrative_prefix_match = bool(
        reference.reference_kind not in {"ACT", "RULE"}
        and normalized and expected.endswith(("/", "-")) and normalized.startswith(expected)
        and reference.reference_date and discovered_date == reference.reference_date
    )
    number_match = exact_number_match or legal_suffix_match or administrative_prefix_match
    date_conflict = bool(reference.reference_date and discovered_date and reference.reference_date != discovered_date)
    issuer_conflict = bool(
        reference.issuer
        and discovered_issuer
        and reference.issuer.casefold() != discovered_issuer.casefold()
    )
    date_match = not date_conflict
    issuer_match = not issuer_conflict
    evidence = {
        "expected_identifier": expected,
        "stored_normalized_identifier": reference.normalized_identifier,
        "discovered_identifier": normalized,
        "number_match": number_match,
        "exact_number_match": exact_number_match,
        "legal_suffix_match": legal_suffix_match,
        "administrative_prefix_match": administrative_prefix_match,
        "date_match": date_match,
        "issuer_match": issuer_match,
        "date_conflict": date_conflict,
        "issuer_conflict": issuer_conflict,
    }
    if not number_match:
        status = "MISMATCH"
    elif date_conflict or issuer_conflict:
        status = "AMBIGUOUS"
    elif reference.reference_kind in {"ACT", "RULE"}:
        status = "MATCH_EXACT"
    elif reference.reference_date and discovered_date == reference.reference_date:
        status = "MATCH_EXACT"
    elif reference.issuer and discovered_issuer and issuer_match:
        status = "MATCH_EXACT"
    else:
        status = "MATCH_STRONG"
    return IdentityMatch(status, normalized, evidence)


def administrative_pdf_identifier(
    reference: ReferenceIdentity, probe_text: str
) -> tuple[str, dict[str, object]]:
    """Find the exact normalized administrative identifier in local PDF text."""
    expected = normalize_document_number(reference.normalized_identifier)
    for line_number, raw_line in enumerate(probe_text.splitlines()[:120], start=1):
        normalized_line = normalize_document_number(raw_line)
        if expected and expected in normalized_line:
            return expected, {
                "local_pdf_identifier_found": True,
                "local_pdf_identifier_line": line_number,
                "local_pdf_identifier_evidence": raw_line.strip()[:500],
            }
    return "", {
        "local_pdf_identifier_found": False,
        "local_pdf_identifier_line": 0,
        "local_pdf_identifier_evidence": "",
    }


def legal_pdf_corroborates(reference: ReferenceIdentity, probe_text: str) -> tuple[bool, dict[str, object]]:
    """Require canonical legal-title tokens and year in the downloaded PDF probe."""
    expected = expected_identifier(reference)
    year_match = re.search(r"_(19|20)\d{2}$", expected)
    year = year_match.group(0)[1:] if year_match else ""
    base = expected[: year_match.start()] if year_match else expected
    ignored = {"ACT", "RULE", "RULES", "THE", "AND", "OF"}
    tokens = [token.casefold() for token in base.split("_") if token and token not in ignored]
    normalized_probe = re.sub(r"[^0-9a-z]+", " ", probe_text.casefold())
    present = [token for token in tokens if re.search(rf"\b{re.escape(token)}s?\b", normalized_probe)]
    required = max(1, (len(tokens) * 3 + 3) // 4) if tokens else 0
    year_present = not year or bool(re.search(rf"\b{re.escape(year)}\b", normalized_probe))
    corroborates = bool(tokens) and len(present) >= required and year_present
    return corroborates, {
        "expected_legal_identifier": expected,
        "required_title_tokens": tokens,
        "matched_title_tokens": present,
        "required_token_count": required,
        "year": year,
        "year_present_in_pdf_probe": year_present,
        "pdf_identity_corroborates": corroborates,
    }


__all__ = [
    "IdentityMatch", "administrative_pdf_identifier", "expected_identifier", "legal_identity_quality", "legal_pdf_corroborates",
    "legal_version_semantics", "verify_identity",
]
