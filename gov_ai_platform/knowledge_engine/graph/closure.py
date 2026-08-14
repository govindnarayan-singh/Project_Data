from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


ELIGIBLE_RESOLUTION_STATUSES = frozenset(
    {"RESOLVED_EXACT", "RESOLVED_STRONG", "AMBIGUOUS", "NOT_IN_CORPUS", "NEEDS_REVIEW"}
)
RESOLVED_STATUSES = frozenset({"RESOLVED_EXACT", "RESOLVED_STRONG"})
LOCAL_RESOLVED_STATUSES = frozenset({"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"})
VALIDATED_RELATION_STATES = frozenset({"AUTO_VERIFIED", "HUMAN_CONFIRMED"})
KIND_EQUIVALENTS = {
    "CIRCULAR": "GOVERNMENT_CIRCULAR",
    "GOVERNMENT_CIRCULAR": "GOVERNMENT_CIRCULAR",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def target_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return an exact, non-fuzzy target identity suitable for closure metrics."""
    identifier = _text(row.get("normalized_document_number"))
    if not identifier:
        return None
    kind = _text(row.get("reference_kind") or "UNKNOWN").upper()
    return KIND_EQUIVALENTS.get(kind, kind), identifier


def eligible_reference_mention(row: dict[str, Any]) -> bool:
    return (
        _text(row.get("resolution_status")) in ELIGIBLE_RESOLUTION_STATUSES
        and target_identity(row) is not None
    )


def _metric(numerator: int, denominator: int) -> dict[str, int | float]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percentage": numerator / denominator if denominator else 0.0,
    }


def build_closure_snapshot(
    candidates: Iterable[dict[str, Any]],
    relations: Iterable[dict[str, Any]],
    *,
    total_documents: int,
    external_verified_targets: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in candidates]
    external_verified_targets = external_verified_targets or set()
    excluded = Counter()
    eligible: list[dict[str, Any]] = []
    for row in rows:
        status = _text(row.get("resolution_status"))
        if status == "SELF_REFERENCE_REJECTED":
            excluded["SELF_REFERENCE_REJECTED"] += 1
        elif status == "UNPARSEABLE":
            excluded["UNPARSEABLE"] += 1
        elif target_identity(row) is None:
            excluded["NO_NORMALIZED_TARGET_IDENTITY"] += 1
        elif status not in ELIGIBLE_RESOLUTION_STATUSES:
            excluded[f"INELIGIBLE_STATUS:{status or 'EMPTY'}"] += 1
        else:
            eligible.append(row)

    mention_rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        identity = target_identity(row)
        assert identity is not None
        groups[identity].append(row)
        ingested = (
            _text(row.get("resolution_status")) in RESOLVED_STATUSES
            and _integer(row.get("resolved_target_document_id")) is not None
        )
        mention_rows.append(
            {
                "candidate_id": _integer(row.get("id")),
                "source_document_id": _integer(row.get("source_document_id")),
                "source_page": _integer(row.get("source_page")),
                "reference_kind": identity[0],
                "normalized_identifier": identity[1],
                "resolution_status": _text(row.get("resolution_status")),
                "resolved_target_document_id": _integer(row.get("resolved_target_document_id")),
                "local_resolution_status": _text(row.get("local_resolution_status")),
                "resolved_target_catalog_id": _integer(row.get("resolved_target_catalog_id")),
                "resolves_to_ingested_document": ingested,
            }
        )

    target_rows: list[dict[str, Any]] = []
    for identity, values in sorted(groups.items()):
        ingested_ids = sorted(
            {
                value
                for row in values
                if _text(row.get("resolution_status")) in RESOLVED_STATUSES
                for value in [_integer(row.get("resolved_target_document_id"))]
                if value is not None
            }
        )
        local_ids = sorted(
            {
                value
                for row in values
                if _text(row.get("local_resolution_status")) in LOCAL_RESOLVED_STATUSES
                for value in [_integer(row.get("resolved_target_catalog_id"))]
                if value is not None
            }
        )
        external_verified = identity in external_verified_targets
        state = (
            "INGESTED"
            if ingested_ids
            else "LOCAL_CATALOGUED"
            if local_ids
            else "EXTERNAL_VERIFIED"
            if external_verified
            else "UNRESOLVED"
        )
        target_rows.append(
            {
                "reference_kind": identity[0],
                "normalized_identifier": identity[1],
                "mentions": len(values),
                "citing_documents": len({_integer(row.get("source_document_id")) for row in values}),
                "availability_state": state,
                "ingested_document_ids": ";".join(map(str, ingested_ids)),
                "local_catalog_ids": ";".join(map(str, local_ids)),
                "external_verified": external_verified,
                "resolution_statuses": ";".join(sorted({_text(row.get("resolution_status")) for row in values})),
            }
        )

    document_relations = [
        row
        for row in relations
        if _text(row.get("relation_type")) != "ISSUED_BY"
        and _integer(row.get("target_document_id")) is not None
        and _text(row.get("validation_status")) in VALIDATED_RELATION_STATES
    ]
    participants = {
        document_id
        for row in document_relations
        for document_id in (
            _integer(row.get("source_document_id")),
            _integer(row.get("target_document_id")),
        )
        if document_id is not None
    }
    target_denominator = len(target_rows)
    mention_numerator = sum(row["resolves_to_ingested_document"] for row in mention_rows)
    ingested_targets = sum(row["availability_state"] == "INGESTED" for row in target_rows)
    locally_available = sum(
        row["availability_state"] in {"INGESTED", "LOCAL_CATALOGUED"} for row in target_rows
    )
    known_available = sum(
        row["availability_state"] in {"INGESTED", "LOCAL_CATALOGUED", "EXTERNAL_VERIFIED"}
        for row in target_rows
    )
    return {
        "summary": {
            "reference_mention_closure": _metric(mention_numerator, len(mention_rows)),
            "referenced_target_closure": _metric(ingested_targets, target_denominator),
            "local_availability_closure": _metric(locally_available, target_denominator),
            "known_availability_closure": _metric(known_available, target_denominator),
            "graph_document_coverage": _metric(len(participants), total_documents),
            "eligible_reference_mentions": len(mention_rows),
            "unique_parseable_target_identities": target_denominator,
            "excluded_mentions": dict(sorted(excluded.items())),
            "target_identity_definition": (
                "Exact canonical reference kind plus exact normalized identifier; circular aliases are "
                "canonicalized, and no fuzzy, vector, title-similarity, date, or department collapse is used."
            ),
            "mention_denominator_definition": (
                "Candidates with an exact normalized target identity and resolution status in "
                "RESOLVED_EXACT, RESOLVED_STRONG, AMBIGUOUS, NOT_IN_CORPUS, or NEEDS_REVIEW."
            ),
            "graph_relation_count": len(document_relations),
            "graph_participant_document_ids": sorted(participants),
        },
        "mention_rows": mention_rows,
        "target_rows": target_rows,
    }


__all__ = [
    "build_closure_snapshot",
    "eligible_reference_mention",
    "target_identity",
]
