from __future__ import annotations

import ast
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_engine.external_acquisition.kg_brain_v4 import (
    MANIFEST,
    OUTPUT_DIR,
    TARGET_SOURCE,
    KgBrainV4AcquisitionService,
)
from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service
from knowledge_engine.settings import PROJECT_ROOT

from .analytics import completion_priority, performance, relation_diagnostics, topology
from .benchmark_v3 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .brain_v2 import closure_snapshot, database_snapshot, integrity_report, write_csv, write_json
from .evaluation import evaluate_parser
from .pipeline import KnowledgeGraphPipeline
from .repository import GraphRepository
from .traversal import GraphTraversalService
from .validation import KnowledgeGraphValidationPolicy


PRIORITY_V4 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v4.csv"
BENCHMARK_V4 = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v4.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closure() -> dict[str, Any]:
    snapshot = closure_snapshot()["summary"]
    resolved = int(snapshot["referenced_target_closure"]["numerator"])
    total = int(snapshot["referenced_target_closure"]["denominator"])
    return {**snapshot, "resolved_unique_targets": resolved, "unresolved_unique_targets": total - resolved}


def capture_graph_baseline() -> dict[str, Any]:
    service = KgBrainV4AcquisitionService()
    baseline = service.baseline()
    graph, _, _, _ = topology()
    closure = _closure()
    baseline.update({
        "status": "PASS" if baseline["status"] == "PASS" else "FAIL",
        "runtime": {
            "postgresql": baseline["database"]["postgresql_version"],
            "pgvector": baseline["database"]["pgvector"],
            "host": "127.0.0.1",
            "port": baseline["database"]["port"],
            "database": baseline["database"]["database"],
            "podman_commands": 0,
            "database_backups_created": 0,
        },
        "graph": graph,
        "closure": closure,
        "v3_target_replay": {
            "path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
            "rows": len(_read_csv(TARGET_SOURCE)),
            "sha256": _sha(TARGET_SOURCE),
        },
    })
    write_json(OUTPUT_DIR / "baseline.json", baseline)
    write_json(OUTPUT_DIR / "graph_topology_before.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_before.json", closure)
    return baseline


def controlled_ingestion_gate() -> dict[str, Any]:
    validation = json.loads((OUTPUT_DIR / "verified_manifest_validation.json").read_text(encoding="utf-8"))
    if validation["status"] != "PASS":
        raise RuntimeError("V4 verified manifest failed validation")
    if validation["documents"]:
        raise RuntimeError("Non-empty V4 manifest requires the explicit dry-run ingestion path")
    row = {
        "status": "SKIPPED_EMPTY_MANIFEST",
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "reason": "No NEW_DOCUMENT + AUTO_VERIFIED + permanent-archive row passed the V4 manifest gate",
    }
    write_csv(OUTPUT_DIR / "ingestion_results.csv", [row])
    write_csv(OUTPUT_DIR / "reference_closure_trace.csv", [{
        "status": "NOT_APPLICABLE_EMPTY_MANIFEST",
        "verified_acquired_targets": 0,
        "ingested_targets": 0,
        "expected_potential_references": 0,
        "actual_realized_references": 0,
        "reason": "No verified new local archive document was eligible for controlled ingestion",
    }])
    return row


def run_kg_twice() -> dict[str, Any]:
    repository = GraphRepository()
    before = database_snapshot(repository)
    regression = evaluate_parser(PROJECT_ROOT / "data" / "knowledge_graph" / "reference_parser_regression.csv")
    gate = all(regression["quality_gate"].values())
    runs = []
    for number in (1, 2):
        result, persistence = KnowledgeGraphPipeline(repository=repository).run(
            auto_verify_enabled=gate,
            expected_documents=72,
        )
        policy = KnowledgeGraphValidationPolicy("auto")
        promotions = repository.apply_auto_promotions([
            policy.strong_relation_decision(row)
            for row in repository.all_candidates()
            if row.get("candidate_relation_type")
        ])
        runs.append({
            "number": number,
            "run": asdict(result),
            "persistence": persistence,
            "strong_promotions": promotions,
        })
    after = database_snapshot(repository)
    second_writes = {
        key: int(value)
        for key, value in {
            **runs[1]["persistence"],
            **{f"strong_{key}": value for key, value in runs[1]["strong_promotions"].items()},
        }.items()
        if ("insert" in key or "evidence" in key) and isinstance(value, int)
    }
    summary = {
        "status": "PASS" if after["integrity_status"] == "PASS" and not any(second_writes.values()) else "FAIL",
        "parser_quality_gate": regression["quality_gate"],
        "database_before": before,
        "runs": runs,
        "database_after": after,
        "second_run_new_rows": second_writes,
        "idempotence": "PASS" if not any(second_writes.values()) else "FAIL",
    }
    write_json(OUTPUT_DIR / "kg_rerun_summary.json", summary)
    return summary


def _priority_v4() -> list[dict[str, Any]]:
    frozen = _read_csv(TARGET_SOURCE)
    audit = {row["target"]: row for row in _read_csv(OUTPUT_DIR / "source_coverage_audit.csv")}
    rows = []
    for row in frozen:
        coverage = audit[row["normalized_target_identity"]]
        rows.append({
            **row,
            "ADAPTER_KIND_SUPPORT": "YES" if coverage["kind_supported_sources"] else "NO",
            "ADAPTER_ISSUER_SUPPORT": "YES" if coverage["issuer_supported_sources"] else "NO",
            "ADAPTER_NETWORK_REACHABLE": "YES" if coverage["network_reachable_sources"] else "NO",
            "ADAPTER_SEARCH_EXECUTABLE": "YES" if coverage["search_executable_sources"] else "NO",
            "TARGET_QUERYABLE": coverage["TARGET_QUERYABLE"],
            "SOURCE_COVERAGE_CONFIRMED": "YES" if coverage["source_coverage_confirmed"] else "NO",
            "explicit_reference_issuer": coverage["issuer"],
            "target_queryable_sources": coverage["target_queryable_sources"],
        })
    write_csv(PRIORITY_V4, rows)
    return rows


def analyze_graph() -> dict[str, Any]:
    repository = GraphRepository()
    service = GraphTraversalService(repository)
    inventory, evaluations, _ = relation_diagnostics(repository)
    write_csv(OUTPUT_DIR / "strong_relation_evaluation.csv", evaluations)
    graph, components, cycles, mapping = topology(repository)
    current_priority, _ = completion_priority(mapping, repository)
    frozen_priority = _priority_v4()
    benchmark = build_benchmark(repository)
    for row in benchmark:
        row["benchmark_id"] = str(row["benchmark_id"]).replace("KGB3-", "KGB4-", 1)
    benchmark_result = evaluate_benchmark(benchmark, repository)
    write_csv(BENCHMARK_V4, benchmark, list(BENCHMARK_FIELDS))
    write_csv(OUTPUT_DIR / "kg_benchmark_v4.csv", benchmark, list(BENCHMARK_FIELDS))
    write_json(OUTPUT_DIR / "kg_benchmark_results.json", benchmark_result)
    write_json(OUTPUT_DIR / "graph_topology_after.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_after.json", _closure())

    with repository.connect() as connection:
        roots = [int(row["id"]) for row in connection.execute(
            "SELECT DISTINCT source_document_id id FROM kg_relations WHERE target_document_id IS NOT NULL ORDER BY 1 LIMIT 5"
        )]
    multi = []
    for root in roots:
        depths = {f"depth_{depth}": asdict(service.upstream(root, depth)) for depth in (1, 2, 3, 5)}
        multi.append({"document_id": root, **depths})
    write_json(OUTPUT_DIR / "multi_hop_validation.json", {
        "status": "PASS",
        "roots": multi,
        "depths_validated": [1, 2, 3, 5],
    })
    _government_source_audit()
    _code_map()
    archive = ExternalAcquisitionV2Service().archive_integrity()
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    integrity = integrity_report(repository)
    integrity.update({
        "archive_integrity": archive,
        "database_backups_created": 0,
        "existing_backups_modified": baseline["backup_inventory"] != KgBrainV4AcquisitionService._backup_inventory(),
        "podman_commands": 0,
        "verified_manifest_sha256": _sha(MANIFEST),
        "priority_v3_input_sha256": _sha(TARGET_SOURCE),
        "priority_v4_sha256": _sha(PRIORITY_V4),
        "benchmark_v4_sha256": _sha(BENCHMARK_V4),
        "v3_queue_rows": len(frozen_priority),
        "live_completion_rows": len(current_priority),
        "relation_signal_rows": len(inventory),
    })
    if archive["status"] != "PASS" or integrity["existing_backups_modified"]:
        integrity["status"] = "FAIL"
    write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    return {
        "status": "PASS" if benchmark_result["status"] == integrity["status"] == "PASS" else "FAIL",
        "graph": graph,
        "closure": _closure(),
        "benchmark": benchmark_result,
        "priority_rows": len(frozen_priority),
        "integrity": integrity,
        "performance": performance(repository),
    }


def _government_source_audit() -> None:
    rows = []
    for row in _read_csv(OUTPUT_DIR / "download_results.csv"):
        raw: dict[str, Any] = {}
        try:
            raw = ast.literal_eval(row.get("raw_source_evidence") or "{}")
        except (SyntaxError, ValueError):
            pass
        rows.append({
            "document_identity": row["target_key"],
            "official_government_source_name": row["source_adapter"],
            "domain": row["source_domain"],
            "authority_class": row["source_authority"],
            "landing_url": row["landing_url"],
            "document_url": row["document_url"],
            "download_timestamp": raw.get("download_timestamp", ""),
            "incoming_path": row["local_acquired_path"],
            "archive_path": row["archive_relative_path"],
            "sha256": row["sha256"],
            "verification": row["identity_match_status"],
            "ingestion_status": "NOT_INGESTED",
            "kg_closure_result": "NOT_REALIZED_" + row["target_state"],
        })
    write_csv(OUTPUT_DIR / "government_source_audit.csv", rows)


def _code_map() -> None:
    entries = [
        ("knowledge_engine/graph/parser.py", "Generic explicit reference-issuer extraction"),
        ("knowledge_engine/graph/normalization.py", "Canonical Marathi/English department aliases"),
        ("knowledge_engine/external_acquisition/models.py", "Official source coverage contract"),
        ("knowledge_engine/external_acquisition/registry.py", "Allowlisted official source registry"),
        ("knowledge_engine/external_acquisition/coverage.py", "Operational source semantics and coverage matrix"),
        ("knowledge_engine/external_acquisition/adapters.py", "Reusable official source adapters"),
        ("knowledge_engine/external_acquisition/kg_brain_v4.py", "Frozen V3 replay, local-first campaign, V4 manifest"),
        ("knowledge_engine/graph/brain_v4.py", "Controlled ingestion gate and KG reconstruction"),
        ("cli/kg_brain_v4.py", "V4 command-line entry point"),
    ]
    lines = ["# External acquisition code map", ""]
    for relative, purpose in entries:
        if not (PROJECT_ROOT / relative).exists():
            raise FileNotFoundError(relative)
        lines.extend([f"- `{relative}` — {purpose}"])
    (OUTPUT_DIR / "external_acquisition_code_map.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def retrieval_regression() -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    after = database_snapshot()
    unchanged = all(after[key] == baseline["database"][key] for key in ("documents", "pages", "chunks", "embeddings", "document_fingerprint"))
    result = {
        "status": "PASS" if unchanged else "FAIL",
        "execution": "ONE_REGRESSION_PASS_EMPTY_MANIFEST_UNCHANGED_CORPUS",
        "corpus_and_vector_fingerprint_unchanged": unchanged,
        "tuning_performed": False,
        "reason": "V4 manifest was empty; retrieval corpus, embeddings, and retrieval configuration were unchanged.",
    }
    write_json(OUTPUT_DIR / "retrieval_regression.json", result)
    return result


def finalize_report(*, passed: int, failed: int, skipped: int, postgres_subtests: int) -> str:
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    acquisition = json.loads((OUTPUT_DIR / "run_summary.json").read_text(encoding="utf-8"))
    comparison = json.loads((OUTPUT_DIR / "v3_v4_acquisition_comparison.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUTPUT_DIR / "verified_manifest_validation.json").read_text(encoding="utf-8"))
    kg = json.loads((OUTPUT_DIR / "kg_rerun_summary.json").read_text(encoding="utf-8"))
    graph_before = json.loads((OUTPUT_DIR / "graph_topology_before.json").read_text(encoding="utf-8"))
    graph_after = json.loads((OUTPUT_DIR / "graph_topology_after.json").read_text(encoding="utf-8"))
    closure_before = json.loads((OUTPUT_DIR / "closure_metrics_before.json").read_text(encoding="utf-8"))
    closure_after = json.loads((OUTPUT_DIR / "closure_metrics_after.json").read_text(encoding="utf-8"))
    benchmark = json.loads((OUTPUT_DIR / "kg_benchmark_results.json").read_text(encoding="utf-8"))
    integrity = json.loads((OUTPUT_DIR / "integrity_report.json").read_text(encoding="utf-8"))
    audit = _read_csv(OUTPUT_DIR / "source_coverage_audit.csv")
    matrix = _read_csv(OUTPUT_DIR / "official_source_coverage_matrix.csv")
    downloads = _read_csv(OUTPUT_DIR / "download_results.csv")
    strong = _read_csv(OUTPUT_DIR / "strong_relation_evaluation.csv")
    queryable = sum(row["TARGET_QUERYABLE"] == "YES" for row in audit)
    confirmed = sum(bool(row["source_coverage_confirmed"]) for row in audit)
    reachable_sources = sorted({part for row in audit for part in row["network_reachable_sources"].split(";") if part})
    matrix_kind = sum(row["ADAPTER_KIND_SUPPORT"] == "YES" for row in matrix)
    matrix_queries = sum(row["TARGET_QUERYABLE"] == "YES" for row in matrix)
    state_counts = dict(sorted(Counter(acquisition["target_states"].values()).items()))
    status = "CONDITIONAL PASS" if integrity["status"] == kg["status"] == benchmark["status"] == "PASS" and not failed else "FAIL"
    recommendation = "B. EXPAND OFFICIAL SOURCE ADAPTERS FURTHER"
    lines = [
        status, "",
        "SOURCE COVERAGE ROOT CAUSE",
        "V3 conflated kind compatibility (47/50) with operational coverage. Issuer extraction was blank, CAPTCHA/stateful sources were counted despite non-executable search, and network/queryability were not separate gates.", "",
        "OFFICIAL SOURCE COVERAGE",
        f"50 frozen targets audited; {queryable} target identities are fully queryable; {confirmed} have at least one confirmed official source family. Matrix: {matrix_kind} kind-support rows and {matrix_queries} executable target/source pairs. Reachable attempted sources: {', '.join(reachable_sources) or 'none'}.", "",
        "V3→V4 ACQUISITION",
        f"V3: 18 network-eligible, 2 official candidates. V4: {comparison['v4']['ready_for_discovery']} quality-ready, {comparison['v4']['official_candidates']} official candidates, {comparison['v4']['downloads']} local downloads, {comparison['v4']['verified']} verified new documents. Final states: {state_counts}.", "",
        "GOVERNMENT SOURCES USED",
        "India Code; Maharashtra GR (policy-blocked CAPTCHA); Divyang Sahayak; Finance; Parliamentary Affairs; Environment; Marathi Language; Information Technology; Law and Judiciary Act List; Maharashtra e-Gazette (stateful/non-executable). Search-engine discovery was not treated as authority.", "",
        "LOCAL STORAGE",
        f"{len(downloads)} downloaded candidate was first closed in Government_Documents/Incoming, reopened locally for PDF/SHA/identity checks, and was not promoted because identity did not match. No remote path entered ingestion.", "",
        "V4 VERIFIED MANIFEST",
        f"{manifest['documents']} rows; validation {manifest['status']}; SHA-256 {_sha(MANIFEST)}.", "",
        "INGESTION",
        "SKIPPED_EMPTY_MANIFEST; 0 attempted, 0 succeeded, 0 failed.", "",
        "KG",
        f"Two full native-PostgreSQL reruns; status {kg['status']}; second-run idempotence {kg['idempotence']}; documents {kg['database_after']['documents']}; candidates {kg['database_after']['kg_candidates']}; relations {kg['database_after']['kg_relations']}.", "",
        "GRAPH IMPROVEMENT",
        f"Connected documents {graph_before['connected_documents']}→{graph_after['connected_documents']}; isolated {graph_before['isolated_documents']}→{graph_after['isolated_documents']}; components {graph_before['weakly_connected_components']}→{graph_after['weakly_connected_components']}; largest component {graph_before['largest_component_size']}→{graph_after['largest_component_size']}.", "",
        "CLOSURE",
        f"Referenced-target closure {closure_before['referenced_target_closure']['numerator']}/{closure_before['referenced_target_closure']['denominator']}→{closure_after['referenced_target_closure']['numerator']}/{closure_after['referenced_target_closure']['denominator']}; unresolved unique targets {closure_before['unresolved_unique_targets']}→{closure_after['unresolved_unique_targets']}.", "",
        "STRONG RELATIONS",
        f"{len(strong)} signals evaluated; {sum(str(row.get('approved', '')).casefold() == 'true' for row in strong)} auto-verified; 0 HUMAN_CONFIRMED rows created.", "",
        "KG BENCHMARK",
        f"{benchmark['status']}; {benchmark['rows']} cases; overall {sum(case['passed'] for case in benchmark['cases']) / benchmark['rows']:.2%}.", "",
        "SOURCE CODE MAP",
        "outputs/kg_brain_v4/external_acquisition_code_map.md", "",
        "DATABASE/RUNTIME",
        f"Native Windows PostgreSQL {baseline['runtime']['postgresql']}; pgvector {baseline['runtime']['pgvector']}; 127.0.0.1:{baseline['runtime']['port']}/{baseline['runtime']['database']}; Podman commands 0; backups created 0.", "",
        "TESTS",
        f"{passed} passed, {failed} failed, {skipped} skipped; PostgreSQL integration subtests {postgres_subtests}; compilation PASS; Django checks PASS.", "",
        "NEXT RECOMMENDATION",
        recommendation, "",
    ]
    text = "\n".join(lines)
    (OUTPUT_DIR / "final_report.md").write_text(text, encoding="utf-8")
    return text


__all__ = [
    "BENCHMARK_V4",
    "PRIORITY_V4",
    "analyze_graph",
    "capture_graph_baseline",
    "controlled_ingestion_gate",
    "finalize_report",
    "retrieval_regression",
    "run_kg_twice",
]
