from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .inventory import resolve_inside_root
from .models import AcquisitionMatch


MANIFEST_FIELDS = (
    "file_path", "department", "document_label", "selection_tier",
    "matched_reference_identifier", "reference_kind", "number_of_citing_documents",
    "number_of_mentions", "relation_signal", "match_status", "match_method",
    "priority_score", "selection_reason",
)


def select_manifest(matches: list[AcquisitionMatch], *, target: int = 120) -> list[dict[str, Any]]:
    candidates: dict[str, tuple[AcquisitionMatch, Any]] = {}
    for match in matches:
        if match.status not in {"MATCH_EXACT", "MATCH_STRONG"} or len(match.candidates) != 1:
            continue
        probe = match.candidates[0]
        existing = candidates.get(probe.sha256)
        if existing is None or match.priority_score > existing[0].priority_score:
            candidates[probe.sha256] = (match, probe)
    ordered = sorted(candidates.values(), key=lambda pair: (-pair[0].priority_score, pair[1].relative_path.casefold()))
    rows = []
    for match, probe in ordered[:target]:
        tier = "A" if match.status == "MATCH_EXACT" else ("C" if match.group.relation_signals else "B")
        rows.append({
            "file_path": probe.relative_path, "department": probe.department,
            "document_label": probe.subject or probe.file_name, "selection_tier": tier,
            "matched_reference_identifier": match.group.normalized_identifier,
            "reference_kind": match.group.reference_kind,
            "number_of_citing_documents": len(match.group.source_documents),
            "number_of_mentions": match.group.mentions,
            "relation_signal": ";".join(match.group.relation_signals),
            "match_status": match.status, "match_method": match.match_method,
            "priority_score": match.priority_score,
            "selection_reason": (
                f"{match.status} identity match for {match.group.mentions} mention(s) from "
                f"{len(match.group.source_documents)} current document(s)"
            ),
        })
    return rows


def validate_manifest(rows: list[dict[str, Any]], *, root: Path, ingested_paths: set[str], ingested_sha: set[str]) -> dict[str, Any]:
    paths: list[str] = []
    hashes: list[str] = []
    missing = outside = not_pdf = existing = 0
    for row in rows:
        try:
            path = resolve_inside_root(root, root / str(row["file_path"]))
        except FileNotFoundError:
            missing += 1
            continue
        except Exception:
            outside += 1
            continue
        if path.suffix.casefold() != ".pdf":
            not_pdf += 1
        relative = path.relative_to(root.resolve()).as_posix()
        paths.append(relative.casefold())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(digest)
        existing += int(relative.casefold() in {value.replace('\\', '/').casefold() for value in ingested_paths} or digest in ingested_sha)
    result = {
        "selected": len(rows), "missing_paths": missing, "outside_root": outside, "not_pdf": not_pdf,
        "duplicate_paths": len(paths) - len(set(paths)), "duplicate_sha": len(hashes) - len(set(hashes)),
        "already_ingested": existing,
    }
    result["status"] = "PASS" if not any(value for key, value in result.items() if key not in {"selected", "status"}) else "FAIL"
    return result
