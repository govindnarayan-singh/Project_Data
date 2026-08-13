from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from knowledge_engine.graph.normalization import normalize_document_number

from .models import UnresolvedGroup


def consolidate_unresolved(rows: list[dict[str, Any]]) -> list[UnresolvedGroup]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("resolution_status") != "NOT_IN_CORPUS":
            continue
        identifier = normalize_document_number(str(row.get("normalized_document_number") or ""))
        if not identifier:
            continue
        grouped[(identifier, str(row.get("reference_kind") or "UNKNOWN"))].append(row)
    result: list[UnresolvedGroup] = []
    for (identifier, kind), items in grouped.items():
        group_id = hashlib.sha256(f"{kind}|{identifier}".encode("utf-8")).hexdigest()[:20]
        dates = sorted({row["normalized_reference_date"] for row in items if row.get("normalized_reference_date")})
        documents = sorted({int(row["source_document_id"]) for row in items})
        result.append(UnresolvedGroup(
            group_id=group_id, normalized_identifier=identifier, reference_kind=kind,
            raw_identifiers=tuple(sorted({str(row.get("raw_document_number") or "") for row in items if row.get("raw_document_number")})),
            reference_dates=tuple(dates),
            source_departments=tuple(sorted({str(row.get("source_department") or "") for row in items if row.get("source_department")})),
            source_documents=tuple(documents),
            source_files=tuple(sorted({str(row.get("source_file") or "") for row in items if row.get("source_file")})),
            source_pages=tuple(sorted({f"{row.get('source_file')}#page={row.get('source_page')}" for row in items})),
            relation_signals=tuple(sorted({str(row.get("candidate_relation_type") or "") for row in items if row.get("candidate_relation_type")})),
            mentions=len(items), high_quality_mentions=sum(not bool(row.get("blocked_by_page_quality")) for row in items),
            blocked_mentions=sum(bool(row.get("blocked_by_page_quality")) for row in items),
        ))
    return sorted(result, key=lambda item: (-len(item.source_documents), -item.mentions, item.reference_kind, item.normalized_identifier))
