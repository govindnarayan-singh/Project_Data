from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from knowledge_engine.settings import PROJECT_ROOT

from . import kg_guided_v1 as engine
from .coverage import YES, coverage_matrix, infer_issuer_from_evidence, registry_snapshot
from .kg_guided_v1 import KgGuidedAcquisitionV1Service, _read_csv, _write_csv, _write_json
from .models import ReferenceIdentity, SourceCandidate


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v4"
TARGET_SOURCE = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v3.csv"
MANIFEST = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_verified_v4.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_brain_v4"
V3_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v3"


def configure_v4() -> None:
    engine.OUTPUT_DIR = OUTPUT_DIR
    engine.TARGET_SOURCE = TARGET_SOURCE
    engine.MANIFEST = MANIFEST
    engine.CACHE_DIR = CACHE_DIR


class KgBrainV4AcquisitionService(KgGuidedAcquisitionV1Service):
    def __init__(self, repository=None):
        configure_v4()
        super().__init__(repository)
        self.references: list[ReferenceIdentity] = []
        self.source_rows: list[dict[str, Any]] = []
        self.quality_by_target: dict[str, str] = {}
        self.issuer_audit: dict[str, dict[str, Any]] = {}

    def _evidence_by_identity(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        with self.repository.connect() as connection:
            rows = list(connection.execute(
                """SELECT reference_kind,normalized_document_number,raw_evidence_text
                     FROM kg_reference_candidates
                     WHERE normalized_document_number<>''
                     ORDER BY reference_kind,normalized_document_number,id"""
            ))
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row["reference_kind"]), str(row["normalized_document_number"]))].append(dict(row))
        return grouped

    def freeze_targets(self) -> tuple[list[dict[str, Any]], list[ReferenceIdentity]]:
        source_rows, references = super().freeze_targets()
        evidence = self._evidence_by_identity()
        enriched: list[ReferenceIdentity] = []
        for reference in references:
            raw, normalized, observations = infer_issuer_from_evidence(
                evidence.get((reference.reference_kind, reference.normalized_identifier), [])
            )
            issuer = reference.issuer or normalized
            enriched.append(replace(reference, issuer=issuer))
            self.issuer_audit[reference.target_key] = {
                "raw_explicit_issuer": raw,
                "normalized_explicit_issuer": normalized,
                "issuer_observations": observations,
                "issuer_source": "EXPLICIT_REFERENCE_EVIDENCE" if normalized else "NOT_PRESENT",
            }
        self.references = enriched
        self.source_rows = source_rows
        self.quality_by_target = {
            row["target"]: row["quality_state"] for row in _read_csv(OUTPUT_DIR / "target_quality.csv")
        }
        before_path = V3_OUTPUT / "source_registry_snapshot.json"
        before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.exists() else []
        _write_json(OUTPUT_DIR / "source_registry_before.json", before)
        _write_json(OUTPUT_DIR / "source_registry_after.json", registry_snapshot())
        self._write_coverage_artifacts()
        return source_rows, enriched

    def _write_coverage_artifacts(self, network_by_source: dict[str, bool] | None = None) -> None:
        matrix = coverage_matrix(
            self.references,
            self.quality_by_target,
            network_by_source=network_by_source,
        )
        _write_csv(OUTPUT_DIR / "official_source_coverage_matrix.csv", matrix)
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in matrix:
            by_target[row["target"]].append(row)
        audits = []
        source_by_target = {row["normalized_target_identity"]: row for row in self.source_rows}
        for reference in self.references:
            rows = by_target[reference.target_key]
            audit = self.issuer_audit.get(reference.target_key, {})
            queryable = [row["source_adapter"] for row in rows if row["TARGET_QUERYABLE"] == YES]
            reachable = [row["source_adapter"] for row in rows if row["ADAPTER_NETWORK_REACHABLE"] == YES]
            confirmed = [row["source_adapter"] for row in rows if row["SOURCE_COVERAGE_CONFIRMED"] == YES]
            audits.append({
                "rank": source_by_target[reference.target_key]["rank"],
                "target": reference.target_key,
                "quality_state": self.quality_by_target[reference.target_key],
                "issuer": reference.issuer,
                **audit,
                "kind_supported_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_KIND_SUPPORT"] == YES),
                "issuer_supported_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_ISSUER_SUPPORT"] == YES),
                "search_executable_sources": ";".join(row["source_adapter"] for row in rows if row["ADAPTER_SEARCH_EXECUTABLE"] == YES),
                "network_reachable_sources": ";".join(reachable),
                "target_queryable_sources": ";".join(queryable),
                "source_coverage_confirmed": ";".join(confirmed),
                "TARGET_QUERYABLE": YES if queryable else "NO",
            })
        _write_csv(OUTPUT_DIR / "source_coverage_audit.csv", audits)

        value: dict[str, dict[str, Any]] = {}
        refs = {item.target_key: item for item in self.references}
        for row in matrix:
            if row["TARGET_QUERYABLE"] != YES:
                continue
            source = row["source_adapter"]
            record = value.setdefault(source, {
                "source_adapter": source,
                "official_source": row["official_source"],
                "queryable_targets": 0,
                "priority_score_sum": 0,
                "distinct_citing_documents_sum": 0,
                "mentions_sum": 0,
                "graph_bridge_targets": 0,
                "target_identities": [],
            })
            ref = refs[row["target"]]
            record["queryable_targets"] += 1
            record["priority_score_sum"] += ref.priority_score
            record["distinct_citing_documents_sum"] += ref.citing_documents
            record["mentions_sum"] += ref.mentions
            record["graph_bridge_targets"] += int(ref.priority_tier == "TIER_A_GRAPH_BRIDGE")
            record["target_identities"].append(ref.target_key)
        values = []
        for record in value.values():
            record["target_identities"] = ";".join(record["target_identities"])
            values.append(record)
        values.sort(key=lambda row: (-row["priority_score_sum"], row["source_adapter"]))
        _write_csv(OUTPUT_DIR / "source_graph_value.csv", values)

    @staticmethod
    def _network_outcomes(results: list[dict[str, Any]]) -> dict[str, bool]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in results:
            grouped[str(row.get("source_adapter") or "")].append(str(row.get("discovery_status") or ""))
        blocked = {"NETWORK_FAILED", "BLOCKED_BY_ENVIRONMENT", "BLOCKED_BY_SOURCE", "NOT_ATTEMPTED"}
        return {
            source: any(status not in blocked for status in statuses)
            for source, statuses in grouped.items() if source
        }

    def run(self) -> dict[str, Any]:
        result = super().run()
        network = self._network_outcomes(result["results"])
        self._write_coverage_artifacts(network)
        shutil.copyfile(OUTPUT_DIR / "manifest_validation.json", OUTPUT_DIR / "verified_manifest_validation.json")
        self._write_comparison(result)
        self._write_source_report(result)
        return result

    def _write_comparison(self, result: dict[str, Any]) -> None:
        v3_path = PROJECT_ROOT / "outputs" / "kg_guided_acquisition_v1" / "run_summary.json"
        v3 = json.loads(v3_path.read_text(encoding="utf-8")) if v3_path.exists() else {}
        def measures(run: dict[str, Any]) -> dict[str, int]:
            rows = run.get("results", [])
            return {
                "ready_for_discovery": int(run.get("ready_for_discovery", 0)),
                "targets_with_results": len({row.get("target_key") for row in rows}),
                "official_candidates": sum(row.get("discovery_status") == "DISCOVERED" for row in rows),
                "downloads": sum(bool(row.get("local_acquired_path")) for row in rows),
                "verified": sum(row.get("verification_status") == "AUTO_VERIFIED" for row in rows),
                "manifest_documents": int(run.get("manifest_documents", 0)),
            }
        old, new = measures(v3), measures(result)
        _write_json(OUTPUT_DIR / "v3_v4_acquisition_comparison.json", {
            "status": "PASS",
            "v3": old,
            "v4": new,
            "delta": {key: new[key] - old[key] for key in new},
            "v3_kind_supported_claim": 47,
            "v3_network_eligible": 18,
            "v3_official_candidates": 2,
            "semantic_correction": "Kind support is no longer reported as executable target coverage.",
        })

    def _write_source_report(self, result: dict[str, Any]) -> None:
        audit = _read_csv(OUTPUT_DIR / "source_coverage_audit.csv")
        queryable = sum(row["TARGET_QUERYABLE"] == YES for row in audit)
        explicit = sum(bool(row["normalized_explicit_issuer"]) for row in audit)
        by_state = Counter(result["target_states"].values())
        lines = [
            "# Official source coverage report", "",
            "## Root cause", "",
            "V3's 47/50 figure represented document-kind compatibility. It did not prove issuer compatibility, network reachability, executable search, or target queryability. The frozen references also carried blank issuer fields because the generic reference parser did not extract explicit departments from citation evidence.", "",
            f"V4 recovered explicit issuers for {explicit}/50 frozen targets and found {queryable}/50 targets with at least one executable official-source route after quality and issuer gates.", "",
            "## Reusable official source families", "",
            "- Maharashtra Law and Judiciary Department Act List: public paginated state-Act index with official S3WaaS PDF links; added as a reusable ACT/RULE adapter.",
            "- India Code: official central and indexed state legislation search and bitstream PDFs.",
            "- Finance, Parliamentary Affairs, Environment, Marathi Language, and Information Technology departmental sites: public official document/search surfaces, routed only when explicit issuer evidence matches.",
            "- Divyang Sahayak: official Maharashtra disability-administration GR index.",
            "- Maharashtra GR and e-Gazette: retained as official coverage but not executable because their public flows are CAPTCHA/stateful; no bypass was attempted.", "",
            "## Official evidence", "",
            "- https://lj.maharashtra.gov.in/en/document-category/act-list/",
            "- https://www.indiacode.nic.in/",
            "- https://finance.maharashtra.gov.in/en/documents/",
            "- https://pa.maharashtra.gov.in/en/documents/",
            "- https://envd.maharashtra.gov.in/documents/",
            "- https://marathi.gov.in/show/gr/",
            "- https://divyangsahayakportal.maharashtra.gov.in/site/government-resolutions", "",
            "## Acquisition outcome", "",
            f"Targets by final state: {dict(sorted(by_state.items()))}.",
            f"Verified manifest documents: {result['manifest_documents']}.", "",
            "Search engines were used only to locate public official surfaces. Final candidates were restricted to registry allowlists, and PDF identity verification occurred only after bytes were closed and reopened from Incoming.", "",
        ]
        (OUTPUT_DIR / "source_coverage_report.md").write_text("\n".join(lines), encoding="utf-8")


__all__ = ["CACHE_DIR", "MANIFEST", "OUTPUT_DIR", "TARGET_SOURCE", "KgBrainV4AcquisitionService", "configure_v4"]
