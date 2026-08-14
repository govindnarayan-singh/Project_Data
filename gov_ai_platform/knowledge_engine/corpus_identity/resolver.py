from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from knowledge_engine.graph.normalization import normalize_department_alias, normalize_document_number

from .models import LocalResolution


RESOLVABLE_CATALOG_STATUSES = {"IDENTIFIED_EXACT", "IDENTIFIED_PARTIAL"}
KIND_EQUIVALENTS = {
    "CIRCULAR": {"CIRCULAR", "GOVERNMENT_CIRCULAR"},
    "GOVERNMENT_CIRCULAR": {"CIRCULAR", "GOVERNMENT_CIRCULAR"},
}


def _group_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        retained_local_resolution = (
            row.get("local_resolution_status") in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}
            and row.get("resolved_target_catalog_id") is not None
        )
        if row.get("resolution_status") != "NOT_IN_CORPUS" and not retained_local_resolution:
            continue
        number = normalize_document_number(str(row.get("normalized_document_number") or ""))
        if not number:
            continue
        groups[(number, str(row.get("reference_kind") or "UNKNOWN"))].append(row)
    result = []
    for (number, kind), items in groups.items():
        result.append(
            {
                "group_id": hashlib.sha256(f"{kind}|{number}".encode("utf-8")).hexdigest()[:20],
                "normalized_identifier": number,
                "reference_kind": kind,
                "candidate_ids": tuple(sorted(int(row["id"]) for row in items)),
                "source_document_ids": tuple(sorted({int(row["source_document_id"]) for row in items})),
                "source_files": tuple(sorted({str(row.get("source_file") or "") for row in items})),
                "reference_dates": tuple(sorted({row["normalized_reference_date"] for row in items if row.get("normalized_reference_date")})),
                "reference_departments": tuple(
                    sorted(
                        {
                            normalize_department_alias(str(row.get("normalized_department") or row.get("raw_department") or ""))
                            for row in items
                            if row.get("normalized_department") or row.get("raw_department")
                        }
                    )
                ),
                "relation_signals": tuple(sorted({str(row.get("candidate_relation_type") or "") for row in items if row.get("candidate_relation_type")})),
                "mentions": len(items),
                "high_quality_mentions": sum(not bool(row.get("blocked_by_page_quality")) for row in items),
                "blocked_mentions": sum(bool(row.get("blocked_by_page_quality")) for row in items),
                "raw_identifiers": tuple(sorted({str(row.get("raw_document_number") or "") for row in items})),
            }
        )
    return sorted(result, key=lambda group: (-len(group["source_document_ids"]), -group["mentions"], group["normalized_identifier"]))


def _dedupe_sha(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sha: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: (item.get("ingested_document_id") is None, str(item["relative_path"]).casefold())):
        by_sha.setdefault(str(row["sha256"]), row)
    return list(by_sha.values())


def _kind_compatible(reference_kind: str, target_kind: str) -> bool:
    if target_kind == "UNKNOWN" or reference_kind == "UNKNOWN":
        return True
    return target_kind in KIND_EQUIVALENTS.get(reference_kind, {reference_kind})


def structural_identifier_key(value: str) -> str:
    """Series/year plus proposal number; excludes the potentially OCR-damaged desk suffix."""
    normalized = normalize_document_number(value)
    parts = normalized.split("/")
    if len(parts) < 2 or re.search(r"(?:19|20)\d{2}$", parts[0]) is None:
        return ""
    proposal_numbers = re.findall(r"\d+", parts[1])
    if not proposal_numbers:
        return ""
    return f"{parts[0]}/{int(proposal_numbers[-1])}"


def _narrow(
    candidates: list[dict[str, Any]],
    *,
    dates: tuple[date, ...],
    departments: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str]:
    if len(candidates) == 1:
        row = candidates[0]
        if dates and row.get("normalized_date") in dates:
            return candidates, "TEXT_DATE"
        if dates and not row.get("normalized_date") and row.get("filename_date") in dates:
            return candidates, "FILENAME_DATE_CORROBORATION"
        if departments and str(row.get("normalized_department") or "") in departments:
            return candidates, "DEPARTMENT"
        return candidates, ""
    if dates:
        exact_date = [row for row in candidates if row.get("normalized_date") in dates]
        if exact_date:
            candidates = exact_date
            if len(candidates) == 1:
                return candidates, "TEXT_DATE"
        filename_date = [row for row in candidates if not row.get("normalized_date") and row.get("filename_date") in dates]
        if not exact_date and filename_date:
            candidates = filename_date
            if len(candidates) == 1:
                return candidates, "FILENAME_DATE_CORROBORATION"
    if departments:
        department = [row for row in candidates if str(row.get("normalized_department") or "") in departments]
        if department:
            candidates = department
            if len(candidates) == 1:
                return candidates, "DEPARTMENT"
    return candidates, ""


def resolve_local_targets(
    candidate_rows: list[dict[str, Any]], catalogue_rows: list[dict[str, Any]]
) -> list[LocalResolution]:
    by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_legal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_structural_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sha_counts = Counter(str(row["sha256"]) for row in catalogue_rows)
    for row in catalogue_rows:
        if row["identity_parse_status"] not in RESOLVABLE_CATALOG_STATUSES:
            continue
        number = normalize_document_number(str(row.get("normalized_document_number") or ""))
        if number:
            by_number[number].append(row)
            structural_key = structural_identifier_key(number)
            if structural_key:
                by_structural_key[structural_key].append(row)
        legal = str(row.get("legal_canonical_key") or "").casefold()
        if legal:
            by_legal[legal].append(row)

    results: list[LocalResolution] = []
    for group in _group_candidates(candidate_rows):
        number = group["normalized_identifier"]
        kind = group["reference_kind"]
        raw_candidates = list(by_number.get(number, []))
        structural_fallback = False
        if kind in {"ACT", "RULE"}:
            raw_candidates.extend(by_legal.get(number.casefold(), []))
        if not raw_candidates:
            structural_key = structural_identifier_key(number)
            if structural_key:
                raw_candidates = list(by_structural_key.get(structural_key, []))
                structural_fallback = bool(raw_candidates)
        unique_ids = {int(row["id"]): row for row in raw_candidates}
        raw_candidates = list(unique_ids.values())
        compatible = [row for row in raw_candidates if _kind_compatible(kind, str(row["document_kind"]))]
        candidates = _dedupe_sha(compatible)

        common = {
            "group_id": group["group_id"],
            "normalized_identifier": number,
            "reference_kind": kind,
            "source_candidate_ids": group["candidate_ids"],
            "source_document_ids": group["source_document_ids"],
            "source_files": group["source_files"],
            "reference_dates": group["reference_dates"],
            "reference_departments": group["reference_departments"],
            "relation_signals": group["relation_signals"],
            "mentions": group["mentions"],
            "high_quality_mentions": group["high_quality_mentions"],
        }
        if group["blocked_mentions"] == group["mentions"]:
            results.append(
                LocalResolution(
                    **common,
                    status="BLOCKED_QUALITY",
                    catalog_id=None,
                    candidate_catalog_ids=tuple(int(row["id"]) for row in candidates),
                    method="SOURCE_PAGE_QUALITY_BLOCK",
                    evidence="Every current KG mention is blocked by the source-page quality policy",
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue
        if not candidates:
            method = "DOCUMENT_KIND_CONFLICT" if raw_candidates else "NO_CATALOG_IDENTITY_MATCH"
            results.append(
                LocalResolution(
                    **common,
                    status="NOT_FOUND_LOCAL",
                    catalog_id=None,
                    candidate_catalog_ids=tuple(int(row["id"]) for row in raw_candidates),
                    method=method,
                    evidence=(
                        f"{len(raw_candidates)} exact-number catalogue row(s) existed but had conflicting document kinds"
                        if raw_candidates
                        else "No catalogued PDF exposes the exact normalized identifier or canonical legal key"
                    ),
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue

        narrowed, corroboration = _narrow(
            candidates,
            dates=group["reference_dates"],
            departments=group["reference_departments"],
        )
        candidate_ids = tuple(sorted(int(row["id"]) for row in narrowed))
        if len(narrowed) != 1:
            results.append(
                LocalResolution(
                    **common,
                    status="AMBIGUOUS_LOCAL",
                    catalog_id=None,
                    candidate_catalog_ids=candidate_ids,
                    method="MULTIPLE_EXACT_LOCAL_IDENTITIES",
                    evidence=f"{len(narrowed)} distinct-SHA local PDFs remain after deterministic date/department narrowing",
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue

        target = narrowed[0]
        target_ingested_id = target.get("ingested_document_id")
        if target_ingested_id is not None and set(group["source_document_ids"]) == {int(target_ingested_id)}:
            results.append(
                LocalResolution(
                    **common,
                    status="SOURCE_UNPARSEABLE",
                    catalog_id=None,
                    candidate_catalog_ids=candidate_ids,
                    method="SELF_REFERENCE_REJECTED_LOCAL",
                    evidence=(
                        f"The only catalogue target is ingested document {target_ingested_id}, which is also every "
                        "mention's source document; page-header/self identity is not an outgoing reference"
                    ),
                    confidence="HIGH",
                    validation_status="REJECTED",
                    ingestion_eligibility="ALREADY_INGESTED",
                )
            )
            continue
        exact_catalogue = target["identity_parse_status"] == "IDENTIFIED_EXACT"
        target_date = target.get("normalized_date")
        filename_date = target.get("filename_date")
        date_conflict = bool(group["reference_dates"] and target_date and target_date not in group["reference_dates"])
        if date_conflict:
            results.append(
                LocalResolution(
                    **common,
                    status="AMBIGUOUS_LOCAL",
                    catalog_id=None,
                    candidate_catalog_ids=candidate_ids,
                    method="EXACT_NUMBER_DATE_CONFLICT",
                    evidence=f"Catalogue text date {target_date} conflicts with reference date(s) {group['reference_dates']}",
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue
        if group["reference_dates"] and not target_date and filename_date and filename_date not in group["reference_dates"]:
            results.append(
                LocalResolution(
                    **common,
                    status="AMBIGUOUS_LOCAL",
                    catalog_id=None,
                    candidate_catalog_ids=candidate_ids,
                    method="REFERENCE_DATE_NOT_CORROBORATED",
                    evidence=(
                        f"The text header date is unparseable and filename date {filename_date} does not corroborate "
                        f"reference date(s) {group['reference_dates']}; filename date is not treated as publication proof"
                    ),
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue
        if (not exact_catalogue or structural_fallback) and not corroboration:
            results.append(
                LocalResolution(
                    **common,
                    status="AMBIGUOUS_LOCAL",
                    catalog_id=None,
                    candidate_catalog_ids=candidate_ids,
                    method="RECONSTRUCTED_IDENTITY_REQUIRES_CORROBORATION",
                    evidence="A partial or structural identifier match lacks independent text/filename date or explicit department corroboration",
                    confidence="LOW",
                    validation_status="UNRESOLVED",
                    ingestion_eligibility="NOT_READY",
                )
            )
            continue

        status = "RESOLVED_LOCAL_EXACT" if exact_catalogue and not structural_fallback and corroboration != "FILENAME_DATE_CORROBORATION" else "RESOLVED_LOCAL_STRONG"
        method = (
            f"STRUCTURAL_IDENTIFIER_{corroboration}"
            if structural_fallback
            else f"EXACT_IDENTIFIER_{corroboration}"
            if corroboration
            else "UNIQUE_EXACT_NORMALIZED_IDENTIFIER"
        )
        duplicate_sha = sha_counts[str(target["sha256"])] > 1
        already_ingested = target.get("ingested_document_id") is not None
        eligibility = "ALREADY_INGESTED" if already_ingested else ("NOT_READY" if duplicate_sha else "READY_FOR_INGESTION")
        results.append(
            LocalResolution(
                **common,
                status=status,
                catalog_id=int(target["id"]),
                candidate_catalog_ids=candidate_ids,
                method=method,
                evidence=(
                    f"catalog_id={target['id']}; path={target['relative_path']}; identity_status={target['identity_parse_status']}; "
                    f"text_date={target_date or ''}; filename_date={target.get('filename_date') or ''}; "
                    f"corroboration={corroboration or 'UNIQUE_NUMBER'}; duplicate_sha={duplicate_sha}"
                ),
                confidence="HIGH",
                validation_status="AUTO_VERIFIED",
                ingestion_eligibility=eligibility,
            )
        )
    return results


__all__ = ["resolve_local_targets"]
