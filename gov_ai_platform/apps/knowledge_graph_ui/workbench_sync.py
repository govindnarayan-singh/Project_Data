from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction

from apps.identity_access.models import OrganizationUnit
from knowledge_engine.graph.normalization import normalize_department_alias, normalize_document_number

from .models import (
    MatchStatus,
    UnresolvedReferenceCandidateLink,
    UnresolvedReferenceWorkItem,
    WorkItemPriority,
    WorkItemStatus,
    WorkItemType,
)


ELIGIBLE_RESOLUTION_STATUSES = frozenset({"NOT_IN_CORPUS", "AMBIGUOUS", "UNPARSEABLE", "NEEDS_REVIEW"})
TERMINAL_WORK_ITEM_STATUSES = frozenset({
    WorkItemStatus.RESOLVED_AUTO,
    WorkItemStatus.RESOLVED_HUMAN,
    WorkItemStatus.CLOSED_INVALID,
})


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _department_key(value: str) -> str:
    key = normalize_department_alias(value or "")
    # The live Stage 1 organization uses the historical "Financial" label,
    # while corpus metadata predominantly uses "Finance".
    return key.replace("financial department", "finance department")


def _identity_key(row: dict[str, Any]) -> str:
    normalized = str(row.get("normalized_document_number") or "").strip()
    if normalized:
        return normalized
    raw = normalize_document_number(str(row.get("raw_document_number") or ""))
    if raw:
        return raw
    evidence = " ".join(str(row.get("raw_evidence_text") or "").casefold().split())[:500]
    return f"unparsed:{hashlib.sha256(evidence.encode('utf-8')).hexdigest()}"


def _target_fingerprint(organization_id: int, row: dict[str, Any]) -> str:
    payload = {
        "organization_id": organization_id,
        "identity": _identity_key(row),
        "kind": str(row.get("reference_kind") or "UNKNOWN"),
        "date": str(row.get("normalized_reference_date") or ""),
        "department": _department_key(str(row.get("normalized_department") or row.get("raw_department") or "")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_candidates() -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id,c.source_document_id,c.source_page,c.reference_kind,c.raw_evidence_text,
                   c.raw_document_number,c.normalized_document_number,c.raw_reference_date,
                   c.normalized_reference_date,c.raw_department,c.normalized_department,
                   c.parser_status,c.resolution_status,c.validation_status,c.fingerprint,
                   c.resolved_target_document_id,c.resolved_target_catalog_id,
                   c.local_resolution_status,c.local_resolution_candidate_count,
                   s.file_name AS source_file,s.department AS source_department
              FROM kg_reference_candidates c
              JOIN documents s ON s.id=c.source_document_id
             WHERE c.resolution_status IN ('NOT_IN_CORPUS','AMBIGUOUS','UNPARSEABLE','NEEDS_REVIEW')
             ORDER BY c.id
            """
        )
        return _rows(cursor)


def _load_catalog() -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,filename,document_kind,normalized_document_number,legal_canonical_key,
                      normalized_date,normalized_department,identity_quality,ingested_document_id
                   FROM local_document_catalog ORDER BY id"""
        )
        return _rows(cursor)


def _load_documents() -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,file_name,document_kind,gr_number,canonical_identity,gr_date,department
                   FROM documents ORDER BY id"""
        )
        return _rows(cursor)


def _organization_map() -> dict[str, OrganizationUnit]:
    result: dict[str, OrganizationUnit] = {}
    for organization in OrganizationUnit.objects.filter(is_active=True).order_by("id"):
        result[_department_key(organization.name)] = organization
        result[_department_key(organization.code)] = organization
    return result


def _catalog_matches(identity: str, kind: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not identity or identity.startswith("unparsed:"):
        return []
    matches = []
    for row in catalog:
        keys = {str(row.get("normalized_document_number") or ""), str(row.get("legal_canonical_key") or "")}
        if identity in keys and (kind == "UNKNOWN" or row.get("document_kind") in {kind, "UNKNOWN"}):
            matches.append(row)
    return matches


def _corpus_matches(identity: str, kind: str, documents: list[dict[str, Any]], source_ids: set[int]) -> list[dict[str, Any]]:
    if not identity or identity.startswith("unparsed:"):
        return []
    matches = []
    for row in documents:
        if int(row["id"]) in source_ids:
            continue
        keys = {
            normalize_document_number(str(row.get("gr_number") or "")),
            normalize_document_number(str(row.get("canonical_identity") or "")),
        }
        if identity in keys and (kind == "UNKNOWN" or row.get("document_kind") in {kind, "UNKNOWN", None, ""}):
            matches.append(row)
    return matches


def _classify_group(group: list[dict[str, Any]], catalog: list[dict[str, Any]], documents: list[dict[str, Any]]) -> dict[str, Any]:
    first = group[0]
    identity = _identity_key(first)
    kind = str(first.get("reference_kind") or "UNKNOWN")
    source_ids = {int(row["source_document_id"]) for row in group}
    local = _catalog_matches(identity, kind, catalog)
    corpus = _corpus_matches(identity, kind, documents, source_ids)
    resolved_local_ids = {
        int(row["resolved_target_catalog_id"])
        for row in group
        if row.get("resolved_target_catalog_id") is not None
        and row.get("local_resolution_status") in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}
    }
    if len(resolved_local_ids) == 1:
        local_ids = sorted(resolved_local_ids)
        local_status = (
            MatchStatus.EXACT
            if all(row.get("local_resolution_status") != "RESOLVED_LOCAL_STRONG" for row in group if row.get("resolved_target_catalog_id"))
            else MatchStatus.STRONG
        )
    else:
        local_ids = [int(row["id"]) for row in local[:25]]
        local_status = MatchStatus.EXACT if len(local) == 1 else (MatchStatus.AMBIGUOUS if local else MatchStatus.NONE)
    corpus_ids = [int(row["id"]) for row in corpus[:25]]
    corpus_status = MatchStatus.EXACT if len(corpus) == 1 else (MatchStatus.AMBIGUOUS if corpus else MatchStatus.NONE)
    statuses = {str(row["resolution_status"]) for row in group}
    if corpus:
        work_type = WorkItemType.DOCUMENT_EXISTS_BUT_UNRESOLVED
    elif "AMBIGUOUS" in statuses:
        work_type = WorkItemType.AMBIGUOUS_MATCH
    elif "UNPARSEABLE" in statuses:
        work_type = WorkItemType.IDENTITY_INCOMPLETE
    elif "NEEDS_REVIEW" in statuses:
        work_type = WorkItemType.PARSING_REVIEW
    else:
        work_type = WorkItemType.MISSING_DOCUMENT
    mentions = len(group)
    priority = WorkItemPriority.HIGH if mentions >= 5 or local_status in {MatchStatus.EXACT, MatchStatus.STRONG} or corpus_status == MatchStatus.EXACT else WorkItemPriority.NORMAL
    display = str(first.get("raw_document_number") or first.get("normalized_document_number") or "").strip()
    if not display:
        display = "Identifier unavailable"
    return {
        "display_identifier": display[:500],
        "normalized_target_identity": identity[:500],
        "reference_kind": kind,
        "target_date": first.get("normalized_reference_date"),
        "target_department": str(first.get("raw_department") or first.get("normalized_department") or "")[:255],
        "work_type": work_type,
        "priority": priority,
        "mention_count": mentions,
        "distinct_source_document_count": len(source_ids),
        "local_match_status": local_status,
        "local_catalog_id": local_ids[0] if len(local_ids) == 1 and local_status in {MatchStatus.EXACT, MatchStatus.STRONG} else None,
        "local_candidate_ids": local_ids,
        "corpus_match_status": corpus_status,
        "corpus_document_id": corpus_ids[0] if len(corpus_ids) == 1 else None,
        "corpus_candidate_ids": corpus_ids,
    }


@dataclass(frozen=True)
class SyncResult:
    eligible_candidates: int
    scoped_candidates: int
    skipped_unmapped_candidates: int
    target_groups: int
    created_work_items: int
    updated_work_items: int
    linked_candidates: int
    reassigned_candidate_links: int
    dry_run: bool

    def as_dict(self) -> dict[str, int | bool]:
        return self.__dict__.copy()


def sync_unresolved_reference_workbench(*, dry_run: bool = False) -> SyncResult:
    candidates = _load_candidates()
    catalog = _load_catalog()
    documents = _load_documents()
    organizations = _organization_map()
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    skipped = 0
    for row in candidates:
        organization = organizations.get(_department_key(str(row.get("source_department") or "")))
        if organization is None:
            skipped += 1
            continue
        grouped[(organization.pk, _target_fingerprint(organization.pk, row))].append(row)

    created = updated = linked = reassigned = 0
    if not dry_run:
        with transaction.atomic():
            org_by_id = {organization.pk: organization for organization in organizations.values()}
            for (organization_id, fingerprint), group in sorted(grouped.items()):
                values = _classify_group(group, catalog, documents)
                item, was_created = UnresolvedReferenceWorkItem.objects.select_for_update().get_or_create(
                    target_fingerprint=fingerprint,
                    defaults={"organization": org_by_id[organization_id], **values},
                )
                if was_created:
                    created += 1
                else:
                    changed = False
                    for field, value in values.items():
                        if getattr(item, field) != value:
                            setattr(item, field, value)
                            changed = True
                    if item.organization_id != organization_id:
                        item.organization_id = organization_id
                        changed = True
                    if changed:
                        item.version += 1
                        item.save()
                        updated += 1
                for row in group:
                    previous = UnresolvedReferenceCandidateLink.objects.filter(candidate_id=int(row["id"])).first()
                    _, link_created = UnresolvedReferenceCandidateLink.objects.update_or_create(
                        candidate_id=int(row["id"]),
                        defaults={
                            "work_item": item,
                            "source_document_id": int(row["source_document_id"]),
                            "source_page": int(row["source_page"]),
                            "candidate_fingerprint": str(row["fingerprint"]),
                        },
                    )
                    linked += int(link_created)
                    reassigned += int(previous is not None and previous.work_item_id != item.pk)
    return SyncResult(
        eligible_candidates=len(candidates),
        scoped_candidates=sum(len(group) for group in grouped.values()),
        skipped_unmapped_candidates=skipped,
        target_groups=len(grouped),
        created_work_items=created,
        updated_work_items=updated,
        linked_candidates=linked,
        reassigned_candidate_links=reassigned,
        dry_run=dry_run,
    )
