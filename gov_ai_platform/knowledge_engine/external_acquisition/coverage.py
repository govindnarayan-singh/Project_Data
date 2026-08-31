from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict
from typing import Any, Iterable

from knowledge_engine.graph.normalization import normalize_department_alias
from knowledge_engine.graph.parser import extract_reference_department

from .models import HistoricalCoverage, ReferenceIdentity, SourceDefinition
from .registry import SOURCE_REGISTRY


YES = "YES"
NO = "NO"
UNKNOWN = "UNKNOWN"


def _year(reference: ReferenceIdentity) -> int | None:
    if reference.reference_date:
        return reference.reference_date.year
    match = re.search(r"(?:19|20)\d{2}", reference.normalized_identifier)
    return int(match.group(0)) if match else None


def issuer_supported(source: SourceDefinition, reference: ReferenceIdentity) -> bool:
    if not source.supported_issuers:
        return True
    issuer = normalize_department_alias(reference.issuer)
    if source.source_id == "maharashtra_law_judiciary" and reference.reference_kind in {"ACT", "RULE"}:
        return not issuer or issuer in source.supported_issuers
    return bool(issuer) and issuer in source.supported_issuers


def jurisdiction_supported(source: SourceDefinition, reference: ReferenceIdentity) -> bool:
    if source.source_id != "maharashtra_law_judiciary":
        return True
    identity = f"{reference.normalized_identifier} {reference.raw_identifier}".casefold()
    return "maharashtra" in identity or "महाराष्ट्र" in identity


def coverage_row(
    source: SourceDefinition,
    reference: ReferenceIdentity,
    *,
    quality_state: str,
    network_reachable: bool | None = None,
) -> dict[str, Any]:
    kind = reference.reference_kind in source.supported_document_kinds
    issuer = issuer_supported(source, reference)
    jurisdiction = jurisdiction_supported(source, reference)
    year = _year(reference)
    year_supported = (
        (source.coverage_start_year is None or year is None or year >= source.coverage_start_year)
        and (source.coverage_end_year is None or year is None or year <= source.coverage_end_year)
    )
    historical_supported = (
        source.historical_coverage == HistoricalCoverage.CONFIRMED
        or (source.historical_coverage == HistoricalCoverage.PARTIAL and year_supported)
        or (
            source.historical_coverage == HistoricalCoverage.CURRENT_ONLY
            and year is not None and year >= 2020
        )
    )
    search = bool(source.enabled and source.automated_discovery and source.search_executable)
    network_gate = network_reachable is not False
    queryable = bool(
        quality_state == "READY_FOR_DISCOVERY"
        and reference.normalized_identifier
        and kind
        and issuer
        and jurisdiction
        and year_supported
        and historical_supported
        and search
        and network_gate
    )
    reachable = UNKNOWN if network_reachable is None else (YES if network_reachable else NO)
    confirmed = bool(
        source.coverage_confirmed and kind and issuer and jurisdiction
        and year_supported and historical_supported
    )
    return {
        "target": reference.target_key,
        "target_kind": reference.reference_kind,
        "target_issuer": reference.issuer,
        "target_year": year or "",
        "quality_state": quality_state,
        "source_adapter": source.source_id,
        "official_source": source.source_name,
        "official_domain": source.base_host,
        "jurisdiction": source.jurisdiction,
        "ADAPTER_KIND_SUPPORT": YES if kind else NO,
        "ADAPTER_ISSUER_SUPPORT": YES if issuer else NO,
        "ADAPTER_JURISDICTION_SUPPORT": YES if jurisdiction else NO,
        "JURISDICTION_SUPPORT": YES if jurisdiction else NO,
        "ADAPTER_YEAR_SUPPORT": YES if year_supported else NO,
        "HISTORICAL_COVERAGE": source.historical_coverage.value,
        "ADAPTER_NETWORK_REACHABLE": reachable,
        "ADAPTER_SEARCH_EXECUTABLE": YES if search else NO,
        "TARGET_QUERYABLE": YES if queryable else NO,
        "SOURCE_COVERAGE_CONFIRMED": YES if confirmed else NO,
        "coverage_evidence_url": source.coverage_evidence_url,
        "block_reason": source.block_reason,
    }


def coverage_matrix(
    references: Iterable[ReferenceIdentity],
    quality_by_target: dict[str, str],
    *,
    network_by_source: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    network = network_by_source or {}
    rows = []
    for reference in references:
        for source in SOURCE_REGISTRY:
            row = coverage_row(
                source,
                reference,
                quality_state=quality_by_target[reference.target_key],
                network_reachable=network.get(source.source_id),
            )
            if row["ADAPTER_KIND_SUPPORT"] == YES or row["ADAPTER_ISSUER_SUPPORT"] == YES:
                rows.append(row)
    return rows


def infer_issuer_from_evidence(evidence_rows: Iterable[dict[str, Any]]) -> tuple[str, str, int]:
    values: Counter[tuple[str, str]] = Counter()
    for row in evidence_rows:
        raw, normalized = extract_reference_department(str(row.get("raw_evidence_text") or ""))
        if normalized:
            values[(raw, normalized)] += 1
    if not values:
        return "", "", 0
    normalized_counts: Counter[str] = Counter()
    for (_, normalized), count in values.items():
        normalized_counts[normalized] += count
    winner, count = normalized_counts.most_common(1)[0]
    if len(normalized_counts) > 1 and normalized_counts.most_common(2)[1][1] == count:
        return "", "", sum(normalized_counts.values())
    raw = next(raw for (raw, normalized), _ in values.most_common() if normalized == winner)
    return raw, winner, sum(normalized_counts.values())


def registry_snapshot() -> list[dict[str, Any]]:
    rows = []
    for source in SOURCE_REGISTRY:
        row = asdict(source)
        row["authority_class"] = source.authority_class.value
        row["supported_strategies"] = [item.value for item in source.supported_strategies]
        rows.append(row)
    return rows


__all__ = [
    "NO",
    "UNKNOWN",
    "YES",
    "coverage_matrix",
    "coverage_row",
    "infer_issuer_from_evidence",
    "issuer_supported",
    "jurisdiction_supported",
    "registry_snapshot",
]
