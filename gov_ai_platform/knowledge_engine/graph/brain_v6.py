from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_engine.external_acquisition.archive import resolve_archive_path
from knowledge_engine.external_acquisition.kg_brain_v4 import KgBrainV4AcquisitionService
from knowledge_engine.external_acquisition.kg_brain_v6 import (
    EXPECTED_TARGET_SHA,
    MANIFEST_V6,
    OUTPUT_DIR,
    PRIORITY_V6,
    TARGET_SOURCE,
    KgBrainV6AcquisitionService,
)
from knowledge_engine.external_acquisition.storage import validate_pdf
from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service
from knowledge_engine.pipelines.batch_ingest import BatchIngestor, ManifestEntry
from knowledge_engine.settings import PROJECT_ROOT, settings

from .analytics import completion_priority, relation_diagnostics, topology
from .benchmark_v3 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .brain_v2 import closure_snapshot, database_snapshot, detailed_retrieval, integrity_report, write_csv, write_json
from .pipeline import KnowledgeGraphPipeline
from .repository import GraphRepository
from .validation import KnowledgeGraphValidationPolicy


V5_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v5"
BENCHMARK_V5 = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v5.csv"
BENCHMARK_V6 = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v6.csv"
RETRIEVAL_BASELINE = PROJECT_ROOT / "outputs" / "retrieval_regression" / "post_reprocess.json"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary_closure(repository: GraphRepository) -> dict[str, Any]:
    result = closure_snapshot(repository)["summary"]
    result["unresolved_unique_targets"] = (
        int(result["referenced_target_closure"]["denominator"])
        - int(result["referenced_target_closure"]["numerator"])
    )
    result["resolved_unique_targets"] = int(result["referenced_target_closure"]["numerator"])
    return result


def capture_baseline() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    repository = GraphRepository()
    database = database_snapshot(repository)
    closure = _summary_closure(repository)
    graph, _nodes, _edges, _mapping = topology(repository)
    target_sha = _sha(TARGET_SOURCE)
    backups = KgBrainV4AcquisitionService._backup_inventory()
    archive = ExternalAcquisitionV2Service().archive_integrity()
    with repository.connect() as connection:
        strong_signals = int(connection.execute(
            "SELECT count(*) n FROM kg_reference_candidates WHERE candidate_relation_type<>''"
        ).fetchone()["n"])
    result = {
        "status": "PASS" if (
            database["integrity_status"] == "PASS" and archive["status"] == "PASS"
            and target_sha == EXPECTED_TARGET_SHA
        ) else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": database, "closure": closure, "graph": graph,
        "strong_relation_signals": strong_signals,
        "archive_integrity": archive, "backup_inventory": backups,
        "runtime": {
            "native_postgresql": True, "host": database["host"], "port": database["port"],
            "database": database["database"], "postgresql": database["postgresql_version"],
            "pgvector": database["pgvector"], "podman_commands": 0,
            "database_backups_created": 0,
        },
        "frozen_priority": {
            "path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
            "rows": len(_read_csv(TARGET_SOURCE)), "sha256": target_sha,
        },
    }
    write_json(OUTPUT_DIR / "baseline.json", result)
    write_json(OUTPUT_DIR / "graph_topology_before.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_before.json", closure)
    if result["status"] != "PASS":
        raise RuntimeError("V6 native database/archive/frozen-target baseline failed")
    return result


def _manifest_entries(*, allow_ingested: bool = False) -> tuple[list[dict[str, str]], list[ManifestEntry]]:
    rows = _read_csv(MANIFEST_V6)
    validation = KgBrainV6AcquisitionService().validate_manifest(rows)
    if validation["status"] != "PASS" and not allow_ingested:
        raise RuntimeError(f"V6 manifest validation failed: {validation['errors']}")
    entries: list[ManifestEntry] = []
    repository = GraphRepository()
    with repository.connect() as connection:
        for row_number, row in enumerate(rows, start=2):
            digest = row["SHA256"]
            candidate = connection.execute(
                """SELECT * FROM external_acquisition_candidates
                     WHERE target_key=%s AND sha256=%s
                     ORDER BY id DESC LIMIT 1""",
                (row["KG target identity"], digest),
            ).fetchone()
            if not candidate:
                raise RuntimeError(f"V6 catalogue provenance missing for {row['KG target identity']}")
            candidate = dict(candidate)
            allowed = {"READY_FOR_INGESTION"}
            if allow_ingested:
                allowed.add("INGESTED")
            if candidate["acquisition_status"] not in allowed:
                raise RuntimeError(f"V6 candidate status is {candidate['acquisition_status']}")
            archive = resolve_archive_path(row["archive_relative_path"], must_exist=True)
            body = archive.read_bytes()
            validate_pdf(body)
            if hashlib.sha256(body).hexdigest() != digest:
                raise RuntimeError("V6 archive SHA changed")
            canonical = row["canonical_identity"]
            year_text = row.get("date/year", "")
            year = next((int(part) for part in year_text.replace("-", " ").split() if len(part) == 4 and part.isdigit()), None)
            department = (
                "Government of India" if row["source_adapter"] == "india_code"
                else "Government of Maharashtra" if row["document_kind"] in {"ACT", "RULE"}
                else row.get("issuer/jurisdiction", "") or "Government of Maharashtra"
            )
            entries.append(ManifestEntry(
                row_number=row_number, file_path=row["archive_relative_path"], department=department,
                document_label=str(candidate.get("discovered_title") or canonical.replace("_", " ").title()),
                notes=row["KG target identity"], absolute_path=archive,
                source_origin="EXTERNAL_OFFICIAL", source_authority=row["source_authority"],
                external_catalogue_id=int(candidate["id"]), document_kind=row["document_kind"],
                canonical_identity=canonical, document_year=year,
                original_acquisition_path=str(candidate.get("local_acquired_path") or ""),
                archive_source_root="EXTERNAL_OFFICIAL", archive_relative_path=row["archive_relative_path"],
                archived_sha256=digest, archived_at=candidate.get("archived_at"),
            ))
    return rows, entries


def controlled_ingestion() -> dict[str, Any]:
    rows = _read_csv(MANIFEST_V6)
    before = database_snapshot()
    if not rows:
        result = {
            "status": "SKIPPED_EMPTY_MANIFEST", "attempted": 0, "succeeded": 0,
            "failed": 0, "dry_run": [], "ingestion": [], "replay": [],
            "database_before": before, "database_after": before,
        }
        write_csv(OUTPUT_DIR / "ingestion_results.csv", [{
            "status": result["status"], "attempted": 0, "succeeded": 0, "failed": 0,
        }])
        write_csv(OUTPUT_DIR / "target_to_edge_closure_trace.csv", [{
            "status": "NOT_APPLICABLE_EMPTY_MANIFEST", "verified_acquired_targets": 0,
            "ingested_targets": 0, "new_REFERENCES": 0,
        }])
        write_json(OUTPUT_DIR / "ingestion_summary.json", result)
        return result

    _rows, entries = _manifest_entries()
    ingestor = BatchIngestor()
    ingestor.preflight(entries)
    dry = [ingestor.process_entry(entry, run_id="kg-brain-v6-dry-run", dry_run=True) for entry in entries]
    if any(item.status not in {"DRY_RUN", "SKIPPED_UNCHANGED"} for item in dry):
        raise RuntimeError("V6 dry-run ingestion failed")
    actual = [ingestor.process_entry(entry, run_id="kg-brain-v6-ingest") for entry in entries]
    success_states = {"SUCCESS", "SUCCESS_WITH_WARNING", "SKIPPED_UNCHANGED"}
    if any(item.status not in success_states for item in actual):
        write_csv(OUTPUT_DIR / "ingestion_results.csv", [asdict(item) for item in actual])
        raise RuntimeError("V6 controlled ingestion failed")
    _rows, replay_entries = _manifest_entries(allow_ingested=True)
    replay = [ingestor.process_entry(entry, run_id="kg-brain-v6-replay") for entry in replay_entries]
    if any(item.status != "SKIPPED_UNCHANGED" for item in replay):
        raise RuntimeError("V6 ingestion replay was not idempotent")
    after = database_snapshot()
    result = {
        "status": "PASS", "attempted": len(actual),
        "succeeded": sum(item.status in success_states for item in actual),
        "failed": sum(item.status not in success_states for item in actual),
        "dry_run": [asdict(item) for item in dry], "ingestion": [asdict(item) for item in actual],
        "replay": [asdict(item) for item in replay], "database_before": before, "database_after": after,
        "source_archive_sha_unchanged": all(
            hashlib.sha256(entry.absolute_path.read_bytes()).hexdigest() == entry.archived_sha256
            for entry in entries
        ),
    }
    write_csv(OUTPUT_DIR / "ingestion_results.csv", [asdict(item) for item in actual])
    write_json(OUTPUT_DIR / "ingestion_summary.json", result)
    return result


def run_kg_twice() -> dict[str, Any]:
    manifest_rows = _read_csv(MANIFEST_V6)
    repository = GraphRepository()
    before = database_snapshot(repository)
    if not manifest_rows:
        result = {
            "status": "SKIPPED_EMPTY_MANIFEST", "runs": [], "database_before": before,
            "database_after": before, "idempotence": "NOT_APPLICABLE",
        }
        write_json(OUTPUT_DIR / "kg_rerun_summary.json", result)
        return result
    runs = []
    expected_documents = int(before["documents"])
    for number in (1, 2):
        pipeline, persistence = KnowledgeGraphPipeline(repository=repository).run(
            auto_verify_enabled=True, expected_documents=expected_documents,
        )
        candidates = repository.all_candidates()
        policy = KnowledgeGraphValidationPolicy("auto")
        decisions = [policy.strong_relation_decision(row) for row in candidates if row.get("candidate_relation_type")]
        promotions = repository.apply_auto_promotions(decisions)
        runs.append({
            "number": number, "run": asdict(pipeline), "persistence": persistence,
            "strong_signals": len(decisions), "strong_approved": sum(item.approved for item in decisions),
            "strong_promotions": promotions,
        })
    after = database_snapshot(repository)
    second = {
        key: int(value) for key, value in runs[1]["persistence"].items()
        if isinstance(value, int) and ("insert" in key or "evidence" in key)
    }
    second.update({
        f"strong_{key}": int(value) for key, value in runs[1]["strong_promotions"].items()
        if isinstance(value, int) and key.startswith("inserted_")
    })
    result = {
        "status": "PASS" if after["integrity_status"] == "PASS" and not any(second.values()) else "FAIL",
        "database_before": before, "database_after": after, "runs": runs,
        "second_run_new_rows": second, "idempotence": "PASS" if not any(second.values()) else "FAIL",
    }
    write_json(OUTPUT_DIR / "kg_rerun_summary.json", result)
    return result


def _target_closure_trace(repository: GraphRepository) -> list[dict[str, Any]]:
    manifest = _read_csv(MANIFEST_V6)
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    rows = []
    with repository.connect() as connection:
        for item in manifest:
            kind, identifier = item["KG target identity"].split("|", 1)
            document = connection.execute(
                "SELECT id FROM documents WHERE sha256=%s", (item["SHA256"],)
            ).fetchone()
            relations = []
            if document:
                relations = list(connection.execute(
                    """SELECT r.id relation_id,r.source_document_id,r.target_document_id,
                              r.validation_status,min(c.source_page) evidence_page
                         FROM kg_relations r LEFT JOIN kg_relation_evidence e ON e.relation_id=r.id
                         LEFT JOIN kg_reference_candidates c ON c.id=e.candidate_id
                        WHERE r.relation_type='REFERENCES' AND r.target_document_id=%s
                        GROUP BY r.id ORDER BY r.id""",
                    (document["id"],),
                ))
            rows.append({
                "priority_target": item["KG target identity"], "source_reference": identifier,
                "official_source": item["source_adapter"], "source_URL": item["source_url"],
                "local_archive_path": item["archive_relative_path"],
                "document_ID": int(document["id"]) if document else "",
                "citing_docs": item["distinct citing documents"],
                "new_REFERENCES": len(relations),
                "relation_evidence_pages": ";".join(str(row["evidence_page"] or "") for row in relations),
                "validation_state": ";".join(sorted({str(row["validation_status"]) for row in relations})),
                "bridge_component_effect": item["potential_component_merges"],
                "baseline_REFERENCES": baseline["database"]["relation_counts"]["REFERENCES"],
            })
    write_csv(OUTPUT_DIR / "target_to_edge_closure_trace.csv", rows)
    return rows


def _priority_v6(repository: GraphRepository, mapping: dict[int, int]) -> list[dict[str, Any]]:
    live, _explanations = completion_priority(mapping, repository)
    coverage = _read_csv(OUTPUT_DIR / "operational_source_coverage.csv")
    attempts = _read_csv(OUTPUT_DIR / "discovery_results.csv")
    by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in coverage:
        by_target[row["target"]].append(row)
    attempt_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in attempts:
        attempt_by_target[row["target_key"]].append(row)
    output = []
    for row in live:
        target = row.get("normalized_target_identity") or row.get("target") or ""
        routes = by_target.get(target, [])
        previous = attempt_by_target.get(target, [])
        output.append({
            **row,
            "confirmed_official_source_routes": ";".join(
                item["source_adapter"] for item in routes if item["SOURCE_COVERAGE_CONFIRMED"] == "YES"
            ),
            "historical_source_coverage": ";".join(sorted({item["HISTORICAL_COVERAGE"] for item in routes})),
            "network_reachability": ";".join(sorted({item["ADAPTER_NETWORK_REACHABLE"] for item in routes})),
            "queryability": "YES" if any(item["TARGET_QUERYABLE"] == "YES" for item in routes) else "NO",
            "previous_acquisition_attempts": len(previous),
            "last_failure": previous[-1].get("target_state", "") if previous else "NOT_ATTEMPTED_V6",
        })
    write_csv(PRIORITY_V6, output)
    return output


def analyze_graph() -> dict[str, Any]:
    repository = GraphRepository()
    database = database_snapshot(repository)
    graph, _nodes, _edges, mapping = topology(repository)
    closure = _summary_closure(repository)
    inventory, evaluations, _summary = relation_diagnostics(repository)
    write_json(OUTPUT_DIR / "graph_topology_after.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_after.json", closure)
    write_csv(OUTPUT_DIR / "strong_relation_target_resolution.csv", evaluations)
    trace = _target_closure_trace(repository)
    priority = _priority_v6(repository, mapping)

    manifest = _read_csv(MANIFEST_V6)
    if manifest:
        old = _read_csv(BENCHMARK_V5)
        current = build_benchmark(repository)
        keys = {(row["query_type"], row["source_document_id"], row["expected_targets"], row["expected_relations"]) for row in old}
        benchmark = list(old)
        for row in current:
            key = (str(row["query_type"]), str(row["source_document_id"]), str(row["expected_targets"]), str(row["expected_relations"]))
            if key not in keys:
                benchmark.append(row)
                keys.add(key)
        for index, row in enumerate(benchmark, 1):
            row["benchmark_id"] = f"KGB6-{index:03d}"
        benchmark_result = evaluate_benchmark(benchmark, repository)
        write_csv(BENCHMARK_V6, benchmark, list(BENCHMARK_FIELDS))
        write_csv(OUTPUT_DIR / "kg_benchmark_v6.csv", benchmark, list(BENCHMARK_FIELDS))
    else:
        benchmark = _read_csv(BENCHMARK_V5)
        benchmark_result = evaluate_benchmark(benchmark, repository)
    write_json(OUTPUT_DIR / "kg_benchmark_results.json", benchmark_result)

    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    backups = KgBrainV4AcquisitionService._backup_inventory()
    archive = ExternalAcquisitionV2Service().archive_integrity()
    integrity = integrity_report(repository)
    integrity.update({
        "archive_integrity": archive, "manifest_sha256": _sha(MANIFEST_V6),
        "priority_v6_sha256": _sha(PRIORITY_V6), "database_backups_created": 0,
        "existing_backups_modified": backups != baseline["backup_inventory"],
        "podman_commands": 0, "remote_byte_identity_processing": 0,
        "permanent_archive_ingestion_only": True,
    })
    if archive["status"] != "PASS" or integrity["existing_backups_modified"]:
        integrity["status"] = "FAIL"
    write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    _update_post_graph_audits(repository, trace)
    _write_code_map()
    return {
        "status": "PASS" if benchmark_result["status"] == integrity["status"] == "PASS" else "FAIL",
        "database": database, "graph": graph, "closure": closure,
        "benchmark": benchmark_result, "priority_rows": len(priority), "integrity": integrity,
        "trace": trace, "strong_relation_signals": len(inventory),
    }


def _update_post_graph_audits(repository: GraphRepository, trace: list[dict[str, Any]]) -> None:
    trace_by_target = {row["priority_target"]: row for row in trace}
    audit = _read_csv(OUTPUT_DIR / "government_source_audit.csv")
    for row in audit:
        target = row["target"]
        item = trace_by_target.get(target)
        if item:
            row["ingestion_status"] = "INGESTED"
            row["document_ID"] = item["document_ID"]
            row["KG_closure_result"] = f"REFERENCES={item['new_REFERENCES']}"
        elif row.get("archive_path"):
            row["ingestion_status"] = "NOT_INGESTED"
            row["KG_closure_result"] = "NO_MANIFEST_INGESTION"
    write_csv(OUTPUT_DIR / "government_source_audit.csv", audit)
    performance = _read_csv(OUTPUT_DIR / "source_family_performance.csv")
    for row in performance:
        source_trace = [item for item in trace if item["official_source"] == row["source_id"]]
        row["ingested"] = sum(bool(item["document_ID"]) for item in source_trace)
        row["KG_targets_closed"] = sum(int(item["new_REFERENCES"]) > 0 for item in source_trace)
    write_csv(OUTPUT_DIR / "source_family_performance.csv", performance)
    comparison_path = OUTPUT_DIR / "v5_v6_comparison.json"
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    comparison["v6"]["ingested"] = sum(bool(item["document_ID"]) for item in trace)
    comparison["v6"]["KG_closures"] = sum(int(item["new_REFERENCES"]) > 0 for item in trace)
    comparison["delta"]["ingested"] = comparison["v6"]["ingested"]
    comparison["delta"]["KG_closures"] = comparison["v6"]["KG_closures"]
    comparison["new_verified_target_closures"] = comparison["v6"]["KG_closures"]
    write_json(comparison_path, comparison)


def retrieval_regression() -> dict[str, Any]:
    manifest = _read_csv(MANIFEST_V6)
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    after = database_snapshot()
    if not manifest:
        keys = ("documents", "pages", "chunks", "embeddings", "document_fingerprint")
        unchanged = all(after[key] == baseline["database"][key] for key in keys)
        result = {
            "status": "PASS" if unchanged else "FAIL", "execution": "FINGERPRINT_ONLY_EMPTY_MANIFEST",
            "dense_benchmark_rerun": False, "corpus_and_vector_fingerprint_unchanged": unchanged,
            "tuning_performed": False,
        }
    else:
        measured = detailed_retrieval()
        historical = json.loads(RETRIEVAL_BASELINE.read_text(encoding="utf-8"))["summary"]
        result = {
            "status": "PASS", "execution": "FROZEN_29_QUERY_DENSE_BENCHMARK_ONCE",
            "dense_benchmark_rerun": True, "question_count": measured["summary"]["question_count"],
            "historical_baseline": historical, "measured": measured["summary"],
            "tuning_performed": False, "hybrid": False, "reranking": False,
            "details": measured["rows"],
        }
    write_json(OUTPUT_DIR / "retrieval_regression.json", result)
    return result


def _write_code_map() -> None:
    entries = [
        ("knowledge_engine/external_acquisition/registry.py", "official source registry and historical/listing declarations"),
        ("knowledge_engine/external_acquisition/adapters.py", "India Code, department, and shared Maharashtra S3WaaS adapters"),
        ("knowledge_engine/external_acquisition/queries.py", "public identifier-only query generation"),
        ("knowledge_engine/external_acquisition/network.py", "SSRF-safe bounded HTTP, redirect, rate, retry, and cache policy"),
        ("knowledge_engine/external_acquisition/storage.py", "atomic Incoming persistence and local PDF validation"),
        ("knowledge_engine/external_acquisition/identity.py", "strict local PDF identity verification"),
        ("knowledge_engine/external_acquisition/archive.py", "byte-identical permanent archive promotion"),
        ("knowledge_engine/external_acquisition/kg_guided_v1.py", "duplicate gates and verified manifest generation"),
        ("knowledge_engine/external_acquisition/kg_brain_v6.py", "V6 freeze, family scoring, routing, coverage, and source performance"),
        ("knowledge_engine/pipelines/batch_ingest.py", "controlled extraction/chunking/BGE-M3/native PostgreSQL ingestion"),
        ("knowledge_engine/graph/brain_v6.py", "V6 ingestion, KG rerun, closure, topology, benchmarks, and audits"),
        ("cli/kg_brain_v6.py", "V6 command-line work-package entry point"),
    ]
    lines = ["# External acquisition code map", ""]
    lines.extend(f"- `{path}` — {purpose}" for path, purpose in entries)
    temporary = (OUTPUT_DIR / "external_acquisition_code_map.md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_DIR / "external_acquisition_code_map.md")


__all__ = [
    "analyze_graph", "capture_baseline", "controlled_ingestion", "retrieval_regression", "run_kg_twice",
]
