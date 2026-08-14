from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from knowledge_engine.acquisition.inventory import resolve_inside_root

from .models import LocalResolution


MANIFEST_FIELDS = (
    "file_path",
    "department",
    "catalog_id",
    "document_kind",
    "raw_document_number",
    "normalized_document_number",
    "document_date",
    "resolution_status",
    "resolution_method",
    "number_of_citing_documents",
    "number_of_mentions",
    "relation_signals",
    "identity_quality",
    "selection_reason",
    "ingestion_eligibility",
    "priority_tier",
    "priority_score",
    "sha256",
)


def build_manifest(
    resolutions: list[LocalResolution], catalogue_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    catalogue = {int(row["id"]): row for row in catalogue_rows}
    by_target: dict[int, list[LocalResolution]] = defaultdict(list)
    for resolution in resolutions:
        if (
            resolution.catalog_id is not None
            and resolution.status in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}
            and resolution.ingestion_eligibility == "READY_FOR_INGESTION"
        ):
            by_target[resolution.catalog_id].append(resolution)

    rows: list[dict[str, Any]] = []
    for catalog_id, items in by_target.items():
        target = catalogue[catalog_id]
        source_documents = {value for item in items for value in item.source_document_ids}
        relation_signals = sorted({value for item in items for value in item.relation_signals})
        mentions = sum(item.mentions for item in items)
        exact = all(item.status == "RESOLVED_LOCAL_EXACT" for item in items)
        if exact and len(source_documents) > 1:
            tier = "A"
        elif exact:
            tier = "B"
        elif any(item.status == "RESOLVED_LOCAL_STRONG" for item in items):
            tier = "C"
        else:
            tier = "D"
        score = (
            len(source_documents) * 100
            + min(mentions, 25) * 10
            + (120 if relation_signals else 0)
            + (400 if exact else 250)
            + (80 if target["normalized_document_number"] else 0)
            + (30 if target.get("normalized_date") else 0)
            + (20 if target.get("normalized_department") else 0)
        )
        methods = sorted({item.method for item in items})
        statuses = sorted({item.status for item in items})
        rows.append(
            {
                "file_path": target["relative_path"],
                "department": target["department_folder"],
                "catalog_id": catalog_id,
                "document_kind": target["document_kind"],
                "raw_document_number": target["raw_document_number"],
                "normalized_document_number": target["normalized_document_number"],
                "document_date": target.get("normalized_date") or "",
                "resolution_status": ";".join(statuses),
                "resolution_method": ";".join(methods),
                "number_of_citing_documents": len(source_documents),
                "number_of_mentions": mentions,
                "relation_signals": ";".join(relation_signals),
                "identity_quality": target["identity_quality"],
                "selection_reason": (
                    f"Unique deterministic local identity covers {mentions} mention(s) "
                    f"from {len(source_documents)} ingested source document(s)"
                ),
                "ingestion_eligibility": "READY_FOR_INGESTION",
                "priority_tier": tier,
                "priority_score": score,
                "sha256": target["sha256"],
            }
        )
    return sorted(rows, key=lambda row: (row["priority_tier"], -row["priority_score"], str(row["file_path"]).casefold()))


def validate_manifest(
    rows: list[dict[str, Any]],
    *,
    root: Path,
    ingested_paths: set[str],
    ingested_sha: set[str],
) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    paths: list[str] = []
    hashes: list[str] = []
    missing = outside = not_pdf = sha_conflicts = already_ingested = 0
    normalized_ingested = {value.replace("\\", "/").casefold() for value in ingested_paths}
    for row in rows:
        try:
            path = resolve_inside_root(resolved_root, resolved_root / str(row["file_path"]))
        except FileNotFoundError:
            missing += 1
            continue
        except Exception:
            outside += 1
            continue
        if path.suffix.casefold() != ".pdf":
            not_pdf += 1
        relative = path.relative_to(resolved_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        paths.append(relative.casefold())
        hashes.append(digest)
        sha_conflicts += int(digest != str(row["sha256"]))
        already_ingested += int(relative.casefold() in normalized_ingested or digest in ingested_sha)
    result = {
        "selected": len(rows),
        "missing_paths": missing,
        "outside_root": outside,
        "not_pdf": not_pdf,
        "sha_conflicts": sha_conflicts,
        "duplicate_paths": len(paths) - len(set(paths)),
        "duplicate_sha": len(hashes) - len(set(hashes)),
        "already_ingested": already_ingested,
    }
    result["status"] = "PASS" if all(value == 0 for key, value in result.items() if key != "selected") else "FAIL"
    return result

