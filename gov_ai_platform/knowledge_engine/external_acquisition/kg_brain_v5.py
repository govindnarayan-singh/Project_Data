from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from knowledge_engine.graph.brain_v5 import MANIFEST_V5, OUTPUT_DIR, TARGET_SOURCE
from knowledge_engine.settings import PROJECT_ROOT

from . import kg_guided_v1 as engine
from .coverage import YES, coverage_matrix, registry_snapshot
from .kg_brain_v4 import KgBrainV4AcquisitionService
from .kg_guided_v1 import KgGuidedAcquisitionV1Service, _read_csv, _write_csv, _write_json, graph_tier
from .models import ReferenceIdentity
from .registry import classify_strategy


CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_brain_v5"
V4_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v4"


def configure_v5() -> None:
    engine.OUTPUT_DIR = OUTPUT_DIR
    engine.TARGET_SOURCE = TARGET_SOURCE
    engine.MANIFEST = MANIFEST_V5
    engine.CACHE_DIR = CACHE_DIR


class KgBrainV5AcquisitionService(KgBrainV4AcquisitionService):
    def __init__(self, repository=None):
        configure_v5()
        super().__init__(repository)
        configure_v5()

    @staticmethod
    def _adapter(definition):
        if definition.source_id in {"maharashtra_social_justice", "maharashtra_minority_development"}:
            from .adapters import MaharashtraDepartmentSource
            return MaharashtraDepartmentSource(definition, cache_dir=CACHE_DIR / definition.source_id)
        return KgBrainV4AcquisitionService._adapter(definition)

    def freeze_targets(self) -> tuple[list[dict[str, Any]], list[ReferenceIdentity]]:
        digest = hashlib.sha256(TARGET_SOURCE.read_bytes()).hexdigest()
        rows = _read_csv(TARGET_SOURCE)
        if digest != "1bff4028b37fbb8117d0c75033f9b36aa29fe303235d06409f6fe49241a363fa" or len(rows) != 50:
            raise ValueError("Frozen V3 50-target queue integrity failed")
        parser_rows = {row["frozen_target"]: row for row in _read_csv(OUTPUT_DIR / "parser_before_after.csv")}
        packets = {row["target"]: row for row in _read_csv(OUTPUT_DIR / "target_evidence_packets.csv")}
        references: list[ReferenceIdentity] = []
        quality_rows: list[dict[str, Any]] = []
        for row in rows:
            target = row["normalized_target_identity"]
            parsed = parser_rows[target]
            packet = packets[target]
            tier, _ = graph_tier(row)
            dates = [value for value in parsed["after_date"].split(";") if value]
            reference_date = date.fromisoformat(dates[0]) if len(dates) == 1 else None
            quality_rows.append({
                "rank": row["rank"], "target": target,
                "quality_state": parsed["after_quality_state"], "reason": parsed["after_quality_reason"],
                "corrected_target": parsed["after_target"],
            })
            references.append(ReferenceIdentity(
                target_key=target, reference_kind=parsed["after_kind"],
                raw_identifier=packet["raw_reference_text"], normalized_identifier=parsed["after_identifier"],
                reference_date=reference_date, issuer=parsed["after_issuer"],
                mentions=int(row.get("mentions") or 0), citing_documents=int(row.get("distinct_citing_documents") or 0),
                relation_signals=tuple(value for value in row.get("relation_signals", "").split(";") if value),
                priority_score=int(float(row.get("priority_score") or 0)), priority_tier=tier,
                strategy=classify_strategy(parsed["after_kind"], parsed["after_identifier"]),
                source_page_quality=row.get("source_quality", ""), local_resolution_state=parsed["after_quality_state"],
            ))
            self.issuer_audit[target] = {
                "raw_explicit_issuer": packet["v2_issuer_evidence_text"],
                "normalized_explicit_issuer": parsed["after_issuer"],
                "issuer_observations": 1 if parsed["after_issuer"] else 0,
                "issuer_source": packet["v2_issuer_state"],
            }
        self.references = references
        self.source_rows = rows
        self.quality_by_target = {row["target"]: row["quality_state"] for row in quality_rows}
        shutil.copyfile(TARGET_SOURCE, OUTPUT_DIR / "target_batch.csv")
        _write_json(OUTPUT_DIR / "target_batch_sha.json", {
            "status": "PASS", "source": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
            "row_count": 50, "sha256": digest, "frozen_membership_and_order": True,
            "identity_overlay": "parser_before_after.csv",
        })
        _write_csv(OUTPUT_DIR / "target_quality.csv", quality_rows)
        before = [
            item for item in json.loads((V4_OUTPUT / "source_registry_after.json").read_text(encoding="utf-8"))
            if item.get("source_id") not in {"maharashtra_social_justice", "maharashtra_minority_development"}
        ]
        _write_json(OUTPUT_DIR / "source_registry_before.json", before)
        _write_json(OUTPUT_DIR / "source_registry_after.json", registry_snapshot())
        self._write_coverage_artifacts()
        return rows, references

    def _write_coverage_artifacts(self, network_by_source: dict[str, bool] | None = None) -> None:
        matrix = coverage_matrix(
            self.references, self.quality_by_target, network_by_source=network_by_source,
        )
        _write_csv(OUTPUT_DIR / "operational_source_coverage.csv", matrix)
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in matrix:
            by_target[row["target"]].append(row)
        source_by_target = {row["normalized_target_identity"]: row for row in self.source_rows}
        audits = []
        for reference in self.references:
            rows = by_target[reference.target_key]
            audit = self.issuer_audit.get(reference.target_key, {})
            queryable = [row["source_adapter"] for row in rows if row["TARGET_QUERYABLE"] == YES]
            audits.append({
                "rank": source_by_target[reference.target_key]["rank"], "target": reference.target_key,
                "quality_state": self.quality_by_target[reference.target_key], "corrected_kind": reference.reference_kind,
                "corrected_identifier": reference.normalized_identifier, "issuer": reference.issuer, **audit,
                "kind_supported_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_KIND_SUPPORT"] == YES),
                "issuer_supported_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_ISSUER_SUPPORT"] == YES),
                "search_executable_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_SEARCH_EXECUTABLE"] == YES),
                "network_reachable_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_NETWORK_REACHABLE"] == YES),
                "target_queryable_sources": ";".join(queryable),
                "source_coverage_confirmed": ";".join(row["source_adapter"] for row in rows if row["SOURCE_COVERAGE_CONFIRMED"] == YES),
                "TARGET_QUERYABLE": YES if queryable else "NO",
            })
        _write_csv(OUTPUT_DIR / "source_coverage_audit.csv", audits)
        values: dict[str, dict[str, Any]] = {}
        refs = {item.target_key: item for item in self.references}
        for row in matrix:
            if row["TARGET_QUERYABLE"] != YES:
                continue
            record = values.setdefault(row["source_adapter"], {
                "source_adapter": row["source_adapter"], "official_source": row["official_source"],
                "unresolved_targets_covered": 0, "distinct_citing_documents": 0, "mentions": 0,
                "graph_components_affected": 0, "bridge_targets_covered": 0,
                "strong_relation_targets_covered": 0, "queryable_targets": 0, "target_identities": [],
            })
            ref = refs[row["target"]]
            record["unresolved_targets_covered"] += 1
            record["distinct_citing_documents"] += ref.citing_documents
            record["mentions"] += ref.mentions
            record["graph_components_affected"] += max(1, ref.citing_documents)
            record["bridge_targets_covered"] += int(ref.priority_tier == "TIER_A_GRAPH_BRIDGE")
            record["strong_relation_targets_covered"] += int(bool(ref.relation_signals))
            record["queryable_targets"] += 1
            record["target_identities"].append(ref.target_key)
        value_rows = []
        for record in values.values():
            record["target_identities"] = ";".join(record["target_identities"])
            value_rows.append(record)
        _write_csv(OUTPUT_DIR / "source_family_graph_value.csv", sorted(value_rows, key=lambda row: (-row["mentions"], row["source_adapter"])))
        maharashtra_rows = [
            {
                "department_or_scope": row["target_issuer"] or "Maharashtra legislation",
                "document_kind": row["target_kind"], "official_source": row["official_source"],
                "official_domain": row["official_domain"], "documents_index": row["coverage_evidence_url"],
                "search_executable": row["ADAPTER_SEARCH_EXECUTABLE"],
                "network_accessible": row["ADAPTER_NETWORK_REACHABLE"],
                "source_coverage_confirmed": row["SOURCE_COVERAGE_CONFIRMED"],
                "captcha_or_stateful": "YES" if "CAPTCHA" in row["block_reason"] or "stateful" in row["block_reason"].casefold() else "NO",
                "historical_coverage": row["ADAPTER_YEAR_SUPPORT"], "block_reason": row["block_reason"],
            }
            for row in matrix if "Maharashtra" in row["jurisdiction"]
        ]
        unique = {(row["department_or_scope"], row["document_kind"], row["official_source"]): row for row in maharashtra_rows}
        _write_csv(OUTPUT_DIR / "maharashtra_source_coverage.csv", list(unique.values()))

    def run(self) -> dict[str, Any]:
        configure_v5()
        result = KgGuidedAcquisitionV1Service.run(self)
        network = self._network_outcomes(result["results"])
        self._write_coverage_artifacts(network)
        shutil.copyfile(OUTPUT_DIR / "manifest_validation.json", OUTPUT_DIR / "verified_manifest_validation.json")
        self._write_v4_v5(result)
        return result

    def _write_v4_v5(self, result: dict[str, Any]) -> None:
        v4 = json.loads((V4_OUTPUT / "run_summary.json").read_text(encoding="utf-8"))
        def values(run: dict[str, Any], quality_path: Path, audit_path: Path, matrix_path: Path) -> dict[str, int]:
            result_rows = run.get("results", [])
            quality = _read_csv(quality_path)
            audit = _read_csv(audit_path)
            matrix = _read_csv(matrix_path)
            states = Counter(run.get("target_states", {}).values())
            return {
                "quality_ready": sum(row["quality_state"] == "READY_FOR_DISCOVERY" for row in quality),
                "queryable_targets": sum(row["TARGET_QUERYABLE"] == YES for row in audit),
                "operational_routes": sum(row["TARGET_QUERYABLE"] == YES for row in matrix),
                "official_candidates": sum(row.get("discovery_status") == "DISCOVERED" for row in result_rows),
                "downloads": sum(bool(row.get("local_acquired_path")) for row in result_rows),
                "match_exact": sum(row.get("identity_match_status") == "MATCH_EXACT" for row in result_rows),
                "match_strong": sum(row.get("identity_match_status") == "MATCH_STRONG" for row in result_rows),
                "auto_verified": sum(row.get("verification_status") == "AUTO_VERIFIED" for row in result_rows),
                "blocked": sum(count for state, count in states.items() if "BLOCKED" in state),
                "ambiguous": sum(count for state, count in states.items() if "AMBIGUOUS" in state),
            }
        old = values(
            v4, V4_OUTPUT / "target_quality.csv", V4_OUTPUT / "source_coverage_audit.csv",
            V4_OUTPUT / "official_source_coverage_matrix.csv",
        )
        new = values(
            result, OUTPUT_DIR / "target_quality.csv", OUTPUT_DIR / "source_coverage_audit.csv",
            OUTPUT_DIR / "operational_source_coverage.csv",
        )
        _write_json(OUTPUT_DIR / "v4_v5_comparison.json", {
            "status": "PASS", "v4": old, "v5": new,
            "delta": {key: new[key] - old[key] for key in new},
            "same_frozen_batch": True,
        })


__all__ = ["CACHE_DIR", "KgBrainV5AcquisitionService", "configure_v5"]
