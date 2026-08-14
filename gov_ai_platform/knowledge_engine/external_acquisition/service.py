from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.closure import target_identity
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.settings import PROJECT_ROOT

from .adapters import BlockedOfficialSource, IndiaCodeSource
from .models import ReferenceIdentity, SourceCandidate
from .registry import SOURCE_REGISTRY, sources_for_kind, validate_registry
from .repository import ExternalAcquisitionRepository


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_phase_v2"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache"
EXTERNAL_MANIFEST = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v1.csv"


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0]) if rows else ["status"])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _json_value(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class ExternalAcquisitionService:
    def __init__(
        self,
        repository: ExternalAcquisitionRepository | None = None,
        graph_repository: GraphRepository | None = None,
    ):
        self.repository = repository or ExternalAcquisitionRepository()
        self.graph_repository = graph_repository or GraphRepository()

    def priority_targets(self, *, limit: int = 20) -> list[ReferenceIdentity]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in self.graph_repository.all_candidates():
            identity = target_identity(row)
            if identity is None or row["resolution_status"] not in {"NOT_IN_CORPUS", "AMBIGUOUS", "NEEDS_REVIEW"}:
                continue
            if row.get("local_resolution_status") in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}:
                continue
            groups[identity].append(row)
        targets: list[ReferenceIdentity] = []
        for (kind, identifier), rows in groups.items():
            if not KnowledgeGraphValidationPolicy.identifier_is_structurally_valid(identifier, kind):
                continue
            dates = sorted({row["normalized_reference_date"] for row in rows if row.get("normalized_reference_date")})
            departments = sorted({str(row.get("normalized_department") or "") for row in rows if row.get("normalized_department")})
            sources = {int(row["source_document_id"]) for row in rows}
            signals = tuple(sorted({str(row.get("candidate_relation_type") or "") for row in rows if row.get("candidate_relation_type")}))
            score = len(sources) * 100 + min(len(rows), 25) * 10 + (120 if signals else 0) + 80
            score += 30 if dates else 0
            score += 20 if departments else 0
            score += min(sum(not bool(row.get("blocked_by_page_quality")) for row in rows), 10) * 3
            raw = Counter(str(row.get("raw_document_number") or "") for row in rows).most_common(1)[0][0]
            targets.append(
                ReferenceIdentity(
                    target_key=f"{kind}|{identifier}", reference_kind=kind, raw_identifier=raw,
                    normalized_identifier=identifier, reference_date=dates[0] if len(dates) == 1 else None,
                    issuer=departments[0] if len(departments) == 1 else "", mentions=len(rows),
                    citing_documents=len(sources), relation_signals=signals, priority_score=score,
                    priority_tier="A" if len(sources) > 1 or len(rows) >= 5 else "B",
                )
            )
        targets.sort(key=lambda item: (-item.priority_score, item.target_key))
        return targets[:limit]

    def _adapter(self, adapter: str):
        definition = next(source for source in SOURCE_REGISTRY if source.adapter == adapter)
        if not definition.automated_discovery:
            return BlockedOfficialSource(definition)
        if adapter == "india_code":
            return IndiaCodeSource(definition, cache_dir=CACHE_DIR / adapter)
        return BlockedOfficialSource(definition)

    def run(self, *, limit: int = 20, live: bool = True) -> dict[str, Any]:
        validate_registry()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        EXTERNAL_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        targets = self.priority_targets(limit=limit)
        run_id = str(uuid.uuid4())
        self.repository.start_run(run_id, len(targets))
        persisted = inserted = 0
        target_rows = []
        try:
            for reference in targets:
                target_rows.append({
                    **{field: _json_value(getattr(reference, field)) for field in reference.__dataclass_fields__},
                    "selection_reason": (
                        f"{reference.citing_documents} citing document(s), {reference.mentions} mention(s), "
                        f"kind={reference.reference_kind}, deterministic identifier complete"
                    ),
                })
                definitions = sources_for_kind(reference.reference_kind)
                if not definitions:
                    continue
                definition = definitions[0]
                adapter = self._adapter(definition.adapter)
                if live:
                    results = adapter.search(reference)
                else:
                    query = reference.raw_identifier.strip() or reference.normalized_identifier
                    results = [
                        SourceCandidate(
                            target_key=reference.target_key,
                            source_adapter=definition.adapter,
                            source_domain=definition.domains[0],
                            source_authority=definition.authority.value,
                            discovery_query=query,
                            discovery_status="BLOCKED",
                            acquisition_status="BLOCKED",
                            verification_status="BLOCKED",
                            raw_source_evidence={
                                "reason": "BLOCKED_BY_EXTERNAL_DATA_APPROVAL",
                                "detail": "No private-corpus-derived query was transmitted externally.",
                            },
                        )
                    ]
                for candidate in results:
                    _, was_inserted = self.repository.upsert(run_id, reference, candidate)
                    persisted += 1
                    inserted += int(was_inserted)
            self.repository.finish_run(run_id, status="SUCCESS", discovered=persisted)
        except Exception as error:
            self.repository.finish_run(run_id, status="FAILED", discovered=persisted, notes=str(error))
            raise

        rows = self.repository.rows(run_id)
        serializable = [{key: _json_value(value) for key, value in row.items()} for row in rows]
        _write_csv(OUTPUT_DIR / "external_target_priority.csv", target_rows)
        _write_csv(OUTPUT_DIR / "external_discovery_results.csv", serializable)
        verified = [row for row in serializable if row["verification_status"] == "AUTO_VERIFIED"]
        ready = [row for row in verified if row["acquisition_status"] == "READY_FOR_INGESTION"]
        ambiguous = [row for row in serializable if row["identity_match_status"] == "AMBIGUOUS"]
        not_found = [row for row in serializable if row["discovery_status"] in {"NOT_FOUND", "NETWORK_FAILED", "BLOCKED"}]
        _write_csv(OUTPUT_DIR / "external_verified_targets.csv", verified)
        _write_csv(OUTPUT_DIR / "external_ambiguous.csv", ambiguous)
        _write_csv(OUTPUT_DIR / "external_not_found.csv", not_found)
        manifest_fields = (
            "local_acquired_path", "source_url", "source_authority", "reference_kind",
            "raw_reference_identifier", "normalized_identifier", "reference_date", "issuer", "target_key",
            "discovered_title", "identity_match_status", "sha256", "priority_tier", "priority_score",
        )
        _write_csv(EXTERNAL_MANIFEST, ready, list(manifest_fields))
        manifest_sha = hashlib.sha256(EXTERNAL_MANIFEST.read_bytes()).hexdigest()
        statuses = Counter(row["discovery_status"] for row in serializable)
        identities = Counter(row["identity_match_status"] for row in serializable)
        acquisitions = Counter(row["acquisition_status"] for row in serializable)
        summary = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": run_id,
            "live_discovery": live,
            "targets_considered": len(groups := self.priority_targets(limit=10000)),
            "targets_selected": len(targets),
            "candidate_rows": len(rows),
            "inserted_rows": inserted,
            "discovery_statuses": dict(statuses),
            "identity_statuses": dict(identities),
            "acquisition_statuses": dict(acquisitions),
            "official_candidates_discovered": sum(row["discovery_status"] == "DISCOVERED" for row in serializable),
            "auto_verified": len(verified),
            "ready_for_ingestion": len(ready),
            "downloaded": sum(bool(row["sha256"]) for row in serializable),
            "official_primary": sum(row["source_authority"] == "OFFICIAL_PRIMARY" for row in serializable),
            "official_secondary": sum(row["source_authority"] == "OFFICIAL_SECONDARY" for row in serializable),
            "non_authoritative_accepted": sum(
                row["source_authority"] == "NON_AUTHORITATIVE" and row["verification_status"] == "AUTO_VERIFIED"
                for row in serializable
            ),
            "manifest": {"path": EXTERNAL_MANIFEST.relative_to(PROJECT_ROOT).as_posix(), "sha256": manifest_sha, "documents": len(ready)},
            "integrity": self.repository.integrity(),
            "source_registry": [
                {"adapter": item.adapter, "base_url": item.base_url, "authority": item.authority.value,
                 "automated_discovery": item.automated_discovery, "block_reason": item.block_reason}
                for item in SOURCE_REGISTRY
            ],
        }
        (OUTPUT_DIR / "external_acquisition_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return summary


__all__ = ["ExternalAcquisitionService"]
