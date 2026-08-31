from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from knowledge_engine.graph.normalization import normalize_department_alias
from knowledge_engine.settings import PROJECT_ROOT, settings

from . import kg_guided_v1 as engine
from .adapters import (
    BlockedOfficialSource,
    DivyangSahayakSource,
    IndiaCodeSource,
    MaharashtraDepartmentSource,
    MaharashtraS3WaaSDocumentSource,
)
from .coverage import YES, coverage_matrix, coverage_row, registry_snapshot
from .kg_brain_v4 import KgBrainV4AcquisitionService
from .kg_guided_v1 import (
    KgGuidedAcquisitionV1Service,
    _read_csv,
    _write_csv,
    _write_json,
    graph_tier,
)
from .models import HistoricalCoverage, ReferenceIdentity
from .registry import SOURCE_REGISTRY, classify_strategy


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v6"
TARGET_SOURCE = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v5.csv"
MANIFEST_V6 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_verified_v6.csv"
PRIORITY_V6 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v6.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_brain_v6"
V5_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v5"
EXPECTED_TARGET_SHA = "6dd4e65f559fd0e00457f9fc2f197bd561667c68273973d81b0873c4aa7c3acb"


def configure_v6() -> None:
    engine.OUTPUT_DIR = OUTPUT_DIR
    engine.TARGET_SOURCE = TARGET_SOURCE
    engine.MANIFEST = MANIFEST_V6
    engine.CACHE_DIR = CACHE_DIR


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def _identity_aliases(kind: str, identity: str) -> set[tuple[str, str]]:
    aliases = {(kind, identity)}
    if kind in {"ACT", "RULE"}:
        aliases.add((kind, identity.removeprefix("THE_")))
        aliases.add((kind, "THE_" + identity.removeprefix("THE_")))
    return aliases


def _source_family(kind: str, identifier: str, issuer: str) -> str:
    if kind in {"ACT", "RULE"}:
        return "maharashtra_state_legislation" if (
            identifier.startswith(("MAHARASHTRA_", "BOMBAY_"))
            or "_MAHARASHTRA_" in identifier
        ) else "central_legislation"
    normalized = normalize_department_alias(issuer)
    return {
        "finance department": "maharashtra_finance_administration",
        "parliamentary affairs department": "maharashtra_parliamentary_affairs",
        "environment department": "maharashtra_environment_administration",
        "marathi language department": "maharashtra_marathi_language",
        "persons with disabilities welfare department": "maharashtra_disability_social_justice",
        "minorities development department": "maharashtra_minority_development",
    }.get(normalized, "maharashtra_administration_unrouted")


class KgBrainV6AcquisitionService(KgBrainV4AcquisitionService):
    def __init__(self, repository=None):
        configure_v6()
        super().__init__(repository)
        configure_v6()

    @staticmethod
    def _adapter(definition):
        cache = CACHE_DIR / definition.source_id
        if definition.source_id == "india_code":
            return IndiaCodeSource(definition, cache_dir=cache)
        if definition.source_id == "divyang_sahayak":
            return DivyangSahayakSource(definition, cache_dir=cache)
        if definition.source_id == "maharashtra_law_judiciary":
            return MaharashtraS3WaaSDocumentSource(definition, cache_dir=cache)
        if definition.source_id.startswith("maharashtra_") and definition.search_executable:
            return MaharashtraDepartmentSource(definition, cache_dir=cache)
        return BlockedOfficialSource(definition)

    def _routes(self, reference: ReferenceIdentity) -> list[Any]:
        routes = []
        for definition in SOURCE_REGISTRY:
            gate = coverage_row(
                definition, reference,
                quality_state=self.quality_by_target.get(reference.target_key, "READY_FOR_DISCOVERY"),
            )
            if gate["TARGET_QUERYABLE"] != YES:
                continue
            adapter = self._adapter(definition)
            if adapter.supports(reference):
                routes.append(adapter)
        return routes

    def freeze_targets(self) -> tuple[list[dict[str, Any]], list[ReferenceIdentity]]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(TARGET_SOURCE.read_bytes()).hexdigest()
        frozen = _read_csv(TARGET_SOURCE)
        if digest != EXPECTED_TARGET_SHA or len(frozen) != 50:
            raise ValueError("Frozen V5 priority queue integrity failed")
        ready = [row for row in frozen if row.get("parser_quality") == "READY_FOR_DISCOVERY"]
        parser_rows = {
            row["frozen_target"]: row
            for row in _read_csv(V5_OUTPUT / "parser_before_after.csv")
        }
        baseline_path = OUTPUT_DIR / "baseline.json"
        baseline_cutoff = None
        if baseline_path.exists():
            baseline_cutoff = json.loads(baseline_path.read_text(encoding="utf-8")).get("generated_at")
        with self.repository.connect() as connection:
            documents = list(connection.execute(
                "SELECT document_kind,canonical_identity,coalesce(gr_number,'') gr_number FROM documents"
            ))
            external = list(connection.execute(
                """SELECT reference_kind,normalized_identifier FROM external_acquisition_candidates
                     WHERE verification_status='AUTO_VERIFIED'
                       AND archive_status='EXTERNAL_OFFICIAL_ARCHIVED'
                       AND (%s::timestamptz IS NULL OR archived_at < %s::timestamptz)""",
                (baseline_cutoff, baseline_cutoff),
            ))
        closed: set[tuple[str, str]] = set()
        for row in documents:
            kind = str(row["document_kind"])
            for identity in (str(row["canonical_identity"] or ""), str(row["gr_number"] or "")):
                if identity:
                    closed.update(_identity_aliases(kind, identity))
        for row in external:
            closed.update(_identity_aliases(str(row["reference_kind"]), str(row["normalized_identifier"])))

        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in ready:
            corrected = row.get("corrected_target_identity") or row["normalized_target_identity"]
            grouped[corrected].append(row)
        subset: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for corrected, rows in grouped.items():
            kind, identifier = corrected.split("|", 1)
            if any(alias in closed for alias in _identity_aliases(kind, identifier)):
                exclusions.append({
                    "target": corrected,
                    "v5_ranks": ";".join(row["rank"] for row in rows),
                    "reason": "ALREADY_INGESTED_OR_ARCHIVED_ALIAS_EXACT",
                })
                continue
            source_ids = sorted({value for row in rows for value in _split(row.get("citing_document_ids", ""))})
            components = sorted({value for row in rows for value in _split(row.get("citing_components", ""))})
            first = min(rows, key=lambda row: int(row["rank"]))
            parsed = parser_rows[first["normalized_target_identity"]]
            issuer = parsed.get("after_issuer", "")
            dates = sorted({value for row in rows for value in _split(row.get("date_evidence", ""))})
            row = dict(first)
            row.update({
                "normalized_target_identity": corrected,
                "corrected_target_identity": corrected,
                "original_v5_target_identities": ";".join(r["normalized_target_identity"] for r in rows),
                "v5_ranks": ";".join(r["rank"] for r in rows),
                "rank": str(min(int(r["rank"]) for r in rows)),
                "raw_variants": ";".join(dict.fromkeys(r.get("raw_variants", "") for r in rows if r.get("raw_variants"))),
                "document_kind": kind,
                "issuer": issuer,
                "date": ";".join(dates),
                "distinct_citing_documents": str(len(source_ids) or max(int(r.get("distinct_citing_documents") or 0) for r in rows)),
                "citing_document_ids": ";".join(source_ids),
                "mentions": str(sum(int(r.get("mentions") or 0) for r in rows)),
                "citing_components": ";".join(components),
                "component_bridge_score": str(max(0, len(components) - 1)),
                "relation_signals": str(sum(int(r.get("strong_relation_signals") or r.get("relation_signals") or 0) for r in rows)),
                "source_family": _source_family(kind, identifier, issuer),
                "parser_quality": "READY_FOR_DISCOVERY",
            })
            subset.append(row)
        subset.sort(key=lambda row: int(row["rank"]))
        if len(ready) != 38 or len(grouped) != 31 or len(subset) != 30:
            raise ValueError(
                f"V6 subset contract changed: ready={len(ready)} unique={len(grouped)} network={len(subset)}"
            )

        references: list[ReferenceIdentity] = []
        quality_rows: list[dict[str, Any]] = []
        for row in subset:
            kind, identifier = row["normalized_target_identity"].split("|", 1)
            dates = _split(row.get("date", ""))
            tier, _flags = graph_tier(row)
            references.append(ReferenceIdentity(
                target_key=row["normalized_target_identity"],
                reference_kind=kind,
                raw_identifier=row.get("raw_variants") or identifier,
                normalized_identifier=identifier,
                reference_date=date.fromisoformat(dates[0]) if len(dates) == 1 else None,
                issuer=row.get("issuer", ""),
                mentions=int(row.get("mentions") or 0),
                citing_documents=int(row.get("distinct_citing_documents") or 0),
                relation_signals=tuple(["SIGNAL"] * int(row.get("relation_signals") or 0)),
                priority_score=int(float(row.get("priority_score") or 0)),
                priority_tier=tier,
                strategy=classify_strategy(kind, identifier),
                source_page_quality=row.get("source_quality", ""),
                local_resolution_state="READY_FOR_DISCOVERY",
            ))
            quality_rows.append({
                "rank": row["rank"], "target": row["normalized_target_identity"],
                "quality_state": "READY_FOR_DISCOVERY",
                "reason": "V5 corrected identity; live V6 exact/alias closure revalidation passed",
            })
            self.issuer_audit[row["normalized_target_identity"]] = {
                "raw_explicit_issuer": row.get("issuer_evidence", ""),
                "normalized_explicit_issuer": row.get("issuer", ""),
                "issuer_observations": int(bool(row.get("issuer", ""))),
                "issuer_source": row.get("issuer_evidence_state", ""),
            }

        _write_csv(OUTPUT_DIR / "target_batch.csv", subset)
        subset_sha = hashlib.sha256((OUTPUT_DIR / "target_batch.csv").read_bytes()).hexdigest()
        _write_json(OUTPUT_DIR / "target_batch_sha.json", {
            "status": "PASS", "source": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
            "source_rows": len(frozen), "source_sha256": digest,
            "ready_rows": len(ready), "unique_corrected_ready_targets": len(grouped),
            "network_execution_targets": len(subset), "already_closed_excluded": len(exclusions),
            "target_batch_sha256": subset_sha, "frozen_membership_and_order": True,
        })
        _write_csv(OUTPUT_DIR / "target_exclusions.csv", exclusions)
        _write_csv(OUTPUT_DIR / "target_quality.csv", quality_rows)
        self.references = references
        self.source_rows = subset
        self.quality_by_target = {row["target"]: row["quality_state"] for row in quality_rows}
        self._write_source_family_artifacts()
        _write_json(OUTPUT_DIR / "source_registry_before.json", json.loads(
            (V5_OUTPUT / "source_registry_after.json").read_text(encoding="utf-8")
        ))
        _write_json(OUTPUT_DIR / "source_registry_after.json", registry_snapshot())
        self._write_coverage_artifacts()
        self._write_source_research()
        return subset, references

    def _write_source_family_artifacts(self) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.source_rows:
            grouped[row["source_family"]].append(row)
        groups: list[dict[str, Any]] = []
        values: list[dict[str, Any]] = []
        adapter_by_family = {
            "maharashtra_state_legislation": "maharashtra_law_judiciary",
            "central_legislation": "india_code",
            "maharashtra_finance_administration": "maharashtra_finance",
            "maharashtra_parliamentary_affairs": "maharashtra_parliamentary_affairs",
            "maharashtra_environment_administration": "maharashtra_environment",
            "maharashtra_marathi_language": "maharashtra_marathi_language",
            "maharashtra_disability_social_justice": "divyang_sahayak;maharashtra_social_justice",
            "maharashtra_minority_development": "maharashtra_minority_development",
            "maharashtra_administration_unrouted": "",
        }
        history = {source.source_id: source.historical_coverage.value for source in SOURCE_REGISTRY}
        for family, rows in grouped.items():
            citing = {value for row in rows for value in _split(row.get("citing_document_ids", ""))}
            bridges = sum(int(row.get("component_bridge_score") or 0) > 0 for row in rows)
            strong = sum(int(row.get("relation_signals") or 0) > 0 for row in rows)
            issuers = sum(bool(row.get("issuer")) or row["document_kind"] in {"ACT", "RULE"} for row in rows)
            adapters = adapter_by_family.get(family, "")
            historical_likelihood = max(
                ({HistoricalCoverage.CONFIRMED.value: 20, HistoricalCoverage.PARTIAL.value: 10,
                  HistoricalCoverage.CURRENT_ONLY.value: 2, HistoricalCoverage.UNKNOWN.value: 0}.get(history.get(adapter, ""), 0)
                 for adapter in _split(adapters)), default=0,
            )
            effort = 5 if adapters else 30
            value = (
                len(rows) * 100 + len(citing) * 25
                + sum(int(row.get("mentions") or 0) for row in rows) * 3
                + bridges * 40 + strong * 50 + len(rows) * 10 + issuers * 10
                + historical_likelihood - effort
            )
            common = {
                "source_family": family,
                "targets": len(rows),
                "target_identities": ";".join(row["normalized_target_identity"] for row in rows),
                "distinct_citing_documents": len(citing),
                "mentions": sum(int(row.get("mentions") or 0) for row in rows),
                "bridge_targets": bridges,
                "strong_relation_targets": strong,
                "current_source_adapter": adapters,
                "source_gap": "NONE" if adapters else "NO_STRICT_ISSUER_ROUTE",
            }
            groups.append(common)
            values.append({
                **common, "identifier_complete": len(rows), "issuer_complete": issuers,
                "historical_coverage_likelihood_points": historical_likelihood,
                "implementation_effort_penalty": effort,
                "SOURCE_FAMILY_VALUE": value,
                "formula": "100*targets + 25*citing_docs + 3*mentions + 40*bridges + 50*strong + 10*identity_complete + 10*issuer_complete + history - effort",
            })
        values.sort(key=lambda row: (-int(row["SOURCE_FAMILY_VALUE"]), row["source_family"]))
        order = {row["source_family"]: index for index, row in enumerate(values)}
        groups.sort(key=lambda row: order[row["source_family"]])
        _write_csv(OUTPUT_DIR / "source_family_groups.csv", groups)
        _write_csv(OUTPUT_DIR / "source_family_graph_value.csv", values)

    def _write_coverage_artifacts(self, network_by_source: dict[str, bool] | None = None) -> None:
        matrix = coverage_matrix(self.references, self.quality_by_target, network_by_source=network_by_source)
        _write_csv(OUTPUT_DIR / "official_source_coverage_matrix.csv", matrix)
        _write_csv(OUTPUT_DIR / "operational_source_coverage.csv", matrix)
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in matrix:
            by_target[row["target"]].append(row)
        audit = []
        source_by_target = {row["normalized_target_identity"]: row for row in self.source_rows}
        for reference in self.references:
            rows = by_target[reference.target_key]
            queryable = [row["source_adapter"] for row in rows if row["TARGET_QUERYABLE"] == YES]
            audit.append({
                "rank": source_by_target[reference.target_key]["rank"],
                "target": reference.target_key,
                "source_family": source_by_target[reference.target_key]["source_family"],
                "issuer": reference.issuer,
                "queryable_routes": len(queryable),
                "target_queryable_sources": ";".join(queryable),
                "TARGET_QUERYABLE": YES if queryable else "NO",
            })
        _write_csv(OUTPUT_DIR / "source_coverage_audit.csv", audit)
        _write_json(OUTPUT_DIR / "operational_coverage_summary.json", {
            "targets": len(audit),
            "queryable_targets": sum(row["TARGET_QUERYABLE"] == YES for row in audit),
            "operational_routes": sum(row["TARGET_QUERYABLE"] == YES for row in matrix),
            "zero_routes": sum(int(row["queryable_routes"]) == 0 for row in audit),
            "one_route": sum(int(row["queryable_routes"]) == 1 for row in audit),
            "multiple_routes": sum(int(row["queryable_routes"]) > 1 for row in audit),
        })

    def _write_source_research(self) -> None:
        rows = []
        catalogue = []
        supported = Counter(
            source_id for row in _read_csv(OUTPUT_DIR / "operational_source_coverage.csv")
            if row["TARGET_QUERYABLE"] == YES for source_id in [row["source_adapter"]]
        )
        for source in SOURCE_REGISTRY:
            captcha = "YES" if "CAPTCHA" in source.block_reason or source.listing_strategy == "CAPTCHA_REQUIRED" else "NO"
            stateful = "YES" if "STATEFUL" in source.listing_strategy or "stateful" in source.block_reason.casefold() else "NO"
            adapter = self._adapter(source)
            common = {
                "source_id": source.source_id, "source_name": source.source_name,
                "official_domain": source.base_host, "authority_class": source.authority_class.value,
                "department": ";".join(source.supported_issuers), "jurisdiction": source.jurisdiction,
                "document_kinds": ";".join(source.supported_document_kinds),
                "historical_coverage": source.historical_coverage.value,
                "search_method": source.discovery_mechanism,
                "listing_strategy": source.listing_strategy,
                "listing_url": source.listing_url,
                "exact_id_search": "YES" if source.search_executable else "NO",
                "date_filtering": "YES" if source.source_id == "divyang_sahayak" else "UNKNOWN",
                "year_browsing": "YES" if source.coverage_start_year else "UNKNOWN",
                "pagination": "YES" if source.listing_max_pages > 1 else "UNKNOWN",
                "sitemap": "NOT_REQUIRED_PUBLIC_INDEX" if source.listing_url else "UNKNOWN",
                "pdf_host_domains": ";".join(source.domains),
                "CAPTCHA": captcha, "stateful": stateful,
                "javascript_dependency": "UNKNOWN", "network_reachable": "UNKNOWN",
                "adapter_file": "knowledge_engine/external_acquisition/adapters.py",
                "adapter_class": type(adapter).__name__,
                "targets_supported": supported[source.source_id],
                "targets_successfully_closed": 0,
                "notes": source.block_reason or source.coverage_evidence_url,
            }
            rows.append(common)
            catalogue.append({key: common[key] for key in (
                "source_id", "source_name", "official_domain", "authority_class", "department",
                "jurisdiction", "document_kinds", "historical_coverage", "search_method",
                "network_reachable", "CAPTCHA", "stateful", "adapter_file", "adapter_class",
                "targets_supported", "targets_successfully_closed", "notes",
            )})
        _write_csv(OUTPUT_DIR / "official_source_research.csv", rows)
        _write_csv(OUTPUT_DIR / "historical_source_coverage.csv", [{
            "source_id": row["source_id"], "official_domain": row["official_domain"],
            "historical_coverage": row["historical_coverage"],
            "evidence": row["notes"], "targets_supported": row["targets_supported"],
        } for row in rows])
        _write_csv(OUTPUT_DIR / "official_source_catalogue.csv", catalogue)

    def run(self) -> dict[str, Any]:
        configure_v6()
        existing_manifest = _read_csv(MANIFEST_V6) if MANIFEST_V6.exists() else []
        result = KgGuidedAcquisitionV1Service.run(self)
        current_manifest = _read_csv(MANIFEST_V6)
        merged = []
        seen_sha: set[str] = set()
        for row in existing_manifest + current_manifest:
            digest = row.get("SHA256", "")
            if digest and digest not in seen_sha:
                merged.append(row)
                seen_sha.add(digest)
        if len(merged) != len(current_manifest):
            _write_csv(MANIFEST_V6, merged, engine.MANIFEST_FIELDS)
            validation = self.validate_manifest(merged)
            _write_json(OUTPUT_DIR / "manifest_validation.json", validation)
            result["manifest_documents"] = len(merged)
            result["manifest_sha256"] = hashlib.sha256(MANIFEST_V6.read_bytes()).hexdigest()
            result["manifest_validation"] = validation
            _write_json(OUTPUT_DIR / "run_summary.json", result)
        else:
            validation = result["manifest_validation"]
        _write_json(OUTPUT_DIR / "verified_manifest_validation.json", validation)
        network = self._network_outcomes(result["results"])
        self._write_coverage_artifacts(network)
        self._write_post_run(result)
        return result

    def _write_post_run(self, result: dict[str, Any]) -> None:
        rows = result["results"]
        performance: list[dict[str, Any]] = []
        targets_by_source = defaultdict(set)
        for matrix in _read_csv(OUTPUT_DIR / "official_source_coverage_matrix.csv"):
            if matrix["ADAPTER_KIND_SUPPORT"] == YES:
                targets_by_source[matrix["source_adapter"]].add(matrix["target"])
        for source in SOURCE_REGISTRY:
            attempts = [row for row in rows if row.get("source_adapter") == source.source_id]
            performance.append({
                "source_id": source.source_id,
                "applicable": len(targets_by_source[source.source_id]),
                "queryable": sum(
                    row["source_adapter"] == source.source_id and row["TARGET_QUERYABLE"] == YES
                    for row in _read_csv(OUTPUT_DIR / "official_source_coverage_matrix.csv")
                ),
                "attempted": len({row.get("target_key") for row in attempts}),
                "candidate": sum(row.get("discovery_status") == "DISCOVERED" for row in attempts),
                "downloaded": sum(bool(row.get("local_acquired_path")) for row in attempts),
                "exact_matches": sum(row.get("identity_match_status") == "MATCH_EXACT" for row in attempts),
                "verified": sum(row.get("verification_status") == "AUTO_VERIFIED" for row in attempts),
                "archived": sum(row.get("archive_status") == "EXTERNAL_OFFICIAL_ARCHIVED" for row in attempts),
                "ingested": 0, "KG_targets_closed": 0,
                "blocked": sum("BLOCKED" in str(row.get("target_state")) for row in attempts),
                "not_found": sum(row.get("target_state") == "NOT_FOUND_AUTHORITATIVE" for row in attempts),
                "failed": sum(row.get("target_state") in {"DOWNLOAD_FAILED", "MISMATCH"} for row in attempts),
            })
        _write_csv(OUTPUT_DIR / "source_family_performance.csv", performance)
        catalogue = _read_csv(OUTPUT_DIR / "official_source_catalogue.csv")
        by_source = {row["source_id"]: row for row in performance}
        network_outcomes = self._network_outcomes(result["results"])
        for row in catalogue:
            network = network_outcomes.get(row["source_id"])
            row["network_reachable"] = "YES" if network is True else ("NO" if network is False else "UNKNOWN")
            row["targets_successfully_closed"] = by_source[row["source_id"]]["archived"]
        _write_csv(OUTPUT_DIR / "official_source_catalogue.csv", catalogue)
        audit = []
        for row in rows:
            raw = row.get("raw_source_evidence") or {}
            if isinstance(raw, str):
                try:
                    raw = ast.literal_eval(raw)
                except (SyntaxError, ValueError):
                    raw = {}
            audit.append({
                "target": row.get("target_key", ""), "government_source": row.get("source_adapter", ""),
                "official_domain": row.get("source_domain", ""), "authority_class": row.get("source_authority", ""),
                "query": row.get("discovery_query", ""), "landing_URL": row.get("landing_url", ""),
                "PDF_URL": row.get("document_url", ""), "download_timestamp": raw.get("download_timestamp", ""),
                "Incoming_path": row.get("local_acquired_path", ""), "SHA": row.get("sha256", ""),
                "identity_status": row.get("identity_match_status", ""),
                "archive_path": row.get("archive_relative_path", ""),
                "ingestion_status": "NOT_YET_INGESTED", "document_ID": "", "KG_closure_result": "PENDING_INGESTION",
            })
        _write_csv(OUTPUT_DIR / "government_source_audit.csv", audit)
        v5 = json.loads((V5_OUTPUT / "run_summary.json").read_text(encoding="utf-8"))
        old_coverage = _read_csv(V5_OUTPUT / "operational_source_coverage.csv")
        new_coverage = _read_csv(OUTPUT_DIR / "operational_source_coverage.csv")
        def measures(run: dict[str, Any], coverage: list[dict[str, str]]) -> dict[str, int]:
            result_rows = run.get("results", [])
            return {
                "ready_targets": int(run.get("ready_for_discovery", 0)),
                "queryable_targets": len({row["target"] for row in coverage if row["TARGET_QUERYABLE"] == YES}),
                "operational_routes": sum(row["TARGET_QUERYABLE"] == YES for row in coverage),
                "official_source_families": len({row["source_adapter"] for row in coverage if row["TARGET_QUERYABLE"] == YES}),
                "official_candidates": sum(row.get("discovery_status") == "DISCOVERED" for row in result_rows),
                "downloads": sum(bool(row.get("local_acquired_path")) for row in result_rows),
                "MATCH_EXACT": sum(row.get("identity_match_status") == "MATCH_EXACT" for row in result_rows),
                "MATCH_STRONG": sum(row.get("identity_match_status") == "MATCH_STRONG" for row in result_rows),
                "new_AUTO_VERIFIED": sum(row.get("verification_status") == "AUTO_VERIFIED" for row in result_rows),
                "archived": sum(row.get("archive_status") == "EXTERNAL_OFFICIAL_ARCHIVED" for row in result_rows),
                "blocked": sum("BLOCKED" in str(row.get("target_state")) for row in result_rows),
                "not_found": sum(row.get("target_state") == "NOT_FOUND_AUTHORITATIVE" for row in result_rows),
                "ambiguous": sum("AMBIGUOUS" in str(row.get("target_state")) for row in result_rows),
            }
        old, new = measures(v5, old_coverage), measures(result, new_coverage)
        _write_json(OUTPUT_DIR / "v5_v6_comparison.json", {
            "status": "PASS", "v5": old, "v6": new,
            "delta": {key: new[key] - old[key] for key in new},
            "new_verified_target_closures": new["archived"],
        })


__all__ = [
    "CACHE_DIR", "EXPECTED_TARGET_SHA", "MANIFEST_V6", "OUTPUT_DIR", "PRIORITY_V6",
    "TARGET_SOURCE", "KgBrainV6AcquisitionService", "configure_v6",
]
