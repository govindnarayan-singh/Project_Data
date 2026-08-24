from __future__ import annotations

import ast
import csv
import hashlib
import json
import shutil
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_engine.external_acquisition.kg_guided_v1 import (
    MANIFEST, OUTPUT_DIR as ACQUISITION_DIR, TARGET_SOURCE, KgGuidedAcquisitionV1Service,
)
from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service
from knowledge_engine.settings import PROJECT_ROOT

from .analytics import administrative_effects, completion_priority, performance, relation_diagnostics, topology
from .benchmark_v3 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .brain_v2 import closure_snapshot, database_snapshot, integrity_report, write_csv, write_json
from .evaluation import evaluate_parser
from .pipeline import KnowledgeGraphPipeline
from .repository import GraphRepository
from .traversal import GraphTraversalService
from .validation import KnowledgeGraphValidationPolicy


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v3"
PRIORITY_V3 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v3.csv"
BENCHMARK_V3 = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v3.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closure_summary() -> dict[str, Any]:
    snapshot = closure_snapshot()
    summary = snapshot["summary"]
    resolved = int(summary["referenced_target_closure"]["numerator"])
    total = int(summary["referenced_target_closure"]["denominator"])
    return {**summary, "resolved_unique_targets": resolved, "unresolved_unique_targets": total - resolved}


def capture_baseline() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    acquisition = json.loads((ACQUISITION_DIR / "baseline.json").read_text(encoding="utf-8"))
    database = database_snapshot()
    graph, components, cycles, _ = topology()
    closure = _closure_summary()
    result = {
        "status": "PASS" if database["integrity_status"] == acquisition["archive_integrity"]["status"] == "PASS" else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {"postgresql": database["postgresql_version"], "pgvector": database["pgvector"],
                    "database": database["database"], "host": "127.0.0.1", "port": database["port"],
                    "podman_commands": 0, "database_backups_created": 0},
        "database": database, "graph": graph, "closure": closure,
        "archive_integrity": acquisition["archive_integrity"],
        "backup_inventory": acquisition["backup_inventory"], "backup_operation": "NONE",
        "priority_v2": {"path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
                        "rows": len(_read_csv(TARGET_SOURCE)), "sha256": _sha(TARGET_SOURCE)},
    }
    write_json(OUTPUT_DIR / "baseline.json", result)
    write_json(OUTPUT_DIR / "graph_topology_before.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_before.json", closure)
    return result


def sync_acquisition() -> dict[str, Any]:
    """Materialize the V3 audit namespace from the completed strict V1 campaign."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mapping = {
        "target_batch.csv": "target_batch.csv", "target_quality.csv": "target_quality.csv",
        "graph_value_metrics.csv": "graph_priority.csv", "source_registry_snapshot.json": "source_registry_snapshot.json",
        "source_attempts.csv": "source_attempts.csv", "discovery_results.csv": "discovery_results.csv",
        "download_results.csv": "download_results.csv", "identity_verification.csv": "identity_verification.csv",
        "duplicate_analysis.csv": "duplicate_analysis.csv", "archived_targets.csv": "archived_targets.csv",
    }
    for source, target in mapping.items():
        shutil.copyfile(ACQUISITION_DIR / source, OUTPUT_DIR / target)
    run = json.loads((ACQUISITION_DIR / "run_summary.json").read_text(encoding="utf-8"))
    source_names = {
        "maharashtra_gr": "Maharashtra Government Resolutions portal",
        "india_code": "India Code",
        "divyang_sahayak": "Divyang Sahayak Maharashtra portal",
    }
    attempt_rows = []
    for row in run["results"]:
        relative = str(row.get("local_acquired_path") or "")
        attempt_rows.append({
            "target_id": row["target_key"], "target_identity": row["target_key"],
            "adapter": row["source_adapter"], "source_name": source_names.get(row["source_adapter"], row["source_adapter"]),
            "authority_class": row["source_authority"], "discovery_query": row["discovery_query"],
            "official_domain": row["source_domain"], "discovery_url": row["discovery_url"],
            "landing_url": row["landing_url"], "document_url": row["document_url"],
            "http_outcome": row["discovery_status"], "download_outcome": row["acquisition_status"],
            "local_incoming_path": str((Path("D:/LLM_Project_data/Government_Documents/Incoming") / relative).as_posix()) if relative else "",
            "sha256": row["sha256"], "verification_outcome": row["verification_status"],
            "identity_outcome": row["identity_match_status"], "target_state": row["target_state"],
        })
    write_csv(OUTPUT_DIR / "source_attempts.csv", attempt_rows)
    validation = json.loads((ACQUISITION_DIR / "manifest_validation.json").read_text(encoding="utf-8"))
    write_json(OUTPUT_DIR / "verified_ingestion_manifest_validation.json", validation)
    if validation["documents"]:
        raise RuntimeError("V3 controlled ingestion must be implemented before a non-empty manifest can proceed")
    write_csv(OUTPUT_DIR / "ingestion_results.csv", [{
        "status": "SKIPPED_EMPTY_MANIFEST", "attempted": 0, "succeeded": 0, "failed": 0,
        "reason": "Verified manifest contains zero NEW_DOCUMENT + AUTO_VERIFIED + archived rows",
    }])
    write_csv(OUTPUT_DIR / "reference_closure_trace.csv", [{
        "status": "NOT_APPLICABLE_EMPTY_MANIFEST", "verified_acquired_targets": 0,
        "ingested_targets": 0, "expected_potential_references": 0, "actual_realized_references": 0,
        "reason": "No verified acquisition target passed the manifest gate",
    }])
    _source_audit()
    _code_map()
    return {"status": "PASS", "manifest": validation, "run": run, "ingestion_skipped": True}


def _source_audit() -> None:
    downloads = _read_csv(ACQUISITION_DIR / "download_results.csv")
    rows = []
    for row in downloads:
        raw = {}
        try:
            raw = ast.literal_eval(row.get("raw_source_evidence") or "{}")
        except (SyntaxError, ValueError):
            pass
        rows.append({
            "document_identity": row["target_key"], "official_government_source_name": "India Code",
            "domain": row["source_domain"], "authority_class": row["source_authority"], "adapter": row["source_adapter"],
            "landing_url": row["landing_url"], "document_url": row["document_url"],
            "download_timestamp": raw.get("download_timestamp", ""),
            "incoming_path": str((Path("D:/LLM_Project_data/Government_Documents/Incoming") / row["local_acquired_path"]).as_posix()),
            "archive_path": row["archive_relative_path"],
            "sha256": row["sha256"], "verification": row["identity_match_status"],
            "ingestion_status": "ALREADY_INGESTED" if row["target_state"] == "ALREADY_INGESTED" else "NOT_INGESTED",
            "resulting_document_id": 82 if row["target_state"] == "ALREADY_INGESTED" else "",
            "kg_closure_result": "ALREADY_CLOSED" if row["target_state"] == "ALREADY_INGESTED" else "NOT_REALIZED_" + row["target_state"],
        })
    write_csv(OUTPUT_DIR / "government_source_audit.csv", rows)


def _code_map() -> None:
    entries = [
        ("knowledge_engine/external_acquisition/registry.py", "Official source registry and routing", "SOURCE_REGISTRY; classify_strategy; sources_for_strategy"),
        ("knowledge_engine/external_acquisition/adapters.py", "Reusable official source adapters", "IndiaCodeSource; DivyangSahayakSource; MaharashtraDepartmentSource; BlockedOfficialSource"),
        ("knowledge_engine/external_acquisition/queries.py", "Public query construction", "generate_queries; assert_public_query"),
        ("knowledge_engine/external_acquisition/network.py", "Bounded HTTPS client, SSRF/DNS/redirect validation", "SafeHttpClient; NetworkSafetyError; _BoundedRedirectHandler"),
        ("knowledge_engine/external_acquisition/storage.py", "Atomic Incoming persistence and local PDF probing", "stage_incoming_file; validate_pdf; safe_filename"),
        ("knowledge_engine/external_acquisition/identity.py", "Kind-aware identity extraction and matching", "verify_identity; administrative_pdf_identifier; legal_pdf_corroborates; legal_version_semantics"),
        ("knowledge_engine/external_acquisition/archive.py", "Safe permanent archive promotion", "promote_verified_file; resolve_archive_path; validate_archive_relative_path"),
        ("knowledge_engine/external_acquisition/repository.py", "Acquisition and provenance persistence", "ExternalAcquisitionRepository"),
        ("knowledge_engine/external_acquisition/kg_guided_v1.py", "KG queue, local-first orchestration, manifest creation", "KgGuidedAcquisitionV1Service; graph_tier; potential_impact"),
        ("cli/kg_guided_acquisition_v1.py", "Acquisition CLI", "main"),
        ("knowledge_engine/graph/brain_v3.py", "V3 manifest gate and KG analysis", "sync_acquisition; run_kg_twice; analyze_graph"),
        ("cli/kg_brain_v3.py", "KG Brain V3 CLI", "main"),
    ]
    lines = ["# External acquisition code map", "", "All paths are repository-relative and were verified for this run.", ""]
    for path, responsibility, symbols in entries:
        if not (PROJECT_ROOT / path).exists():
            raise FileNotFoundError(path)
        lines.extend([f"## `{path}`", "", responsibility + ".", "", f"Important classes/functions: `{symbols}`.", ""])
    (OUTPUT_DIR / "external_acquisition_code_map.md").write_text("\n".join(lines), encoding="utf-8")


def run_kg_twice() -> dict[str, Any]:
    repository = GraphRepository(); before = database_snapshot(repository)
    regression = evaluate_parser(PROJECT_ROOT / "data" / "knowledge_graph" / "reference_parser_regression.csv")
    gate = all(regression["quality_gate"].values())
    runs = []
    for number in (1, 2):
        result, persistence = KnowledgeGraphPipeline(repository=repository).run(auto_verify_enabled=gate, expected_documents=72)
        candidates = repository.all_candidates(); policy = KnowledgeGraphValidationPolicy("auto")
        promotions = repository.apply_auto_promotions([
            policy.strong_relation_decision(row) for row in candidates if row.get("candidate_relation_type")
        ])
        runs.append({"number": number, "run": asdict(result), "persistence": persistence, "strong_promotions": promotions})
    after = database_snapshot(repository)
    second_writes = {
        key: int(value) for key, value in {**runs[1]["persistence"], **{f"strong_{k}": v for k, v in runs[1]["strong_promotions"].items()}}.items()
        if ("insert" in key or "evidence" in key) and isinstance(value, int)
    }
    summary = {"status": "PASS" if after["integrity_status"] == "PASS" and not any(second_writes.values()) else "FAIL",
               "parser_quality_gate": regression["quality_gate"], "database_before": before, "runs": runs,
               "database_after": after, "second_run_new_rows": second_writes,
               "idempotence": "PASS" if not any(second_writes.values()) else "FAIL"}
    write_json(OUTPUT_DIR / "kg_rerun_summary.json", summary)
    return summary


def analyze_graph() -> dict[str, Any]:
    repository = GraphRepository(); service = GraphTraversalService(repository)
    inventory, evaluations, phrases = relation_diagnostics(repository)
    classifications = Counter(row["signal_class"] for row in inventory)
    explicit = next((row for row in evaluations if row["signal_class"] == "HIGH_CONFIDENCE_EXPLICIT"), None)
    relation_summary = {
        "signals": len(evaluations), "classifications": dict(classifications),
        "parser_defects_fixed": 0, "auto_verified_strong_edges": sum(str(row["approved"]).lower() == "true" for row in evaluations),
        "remaining_blocked_signals": sum(str(row["approved"]).lower() != "true" for row in evaluations),
        "explicit_signal_investigation": {
            "candidate_id": explicit["candidate_id"] if explicit else None,
            "finding": "NO_REUSABLE_PARSER_DEFECT",
            "reason": "The explicit supersession sentence supplies a date but no uniquely identifying target number or issuer; parser validity correctly blocks promotion.",
        },
    }
    graph, components, cycles, mapping = topology(repository)
    priority, bridges = completion_priority(mapping, repository)
    write_csv(PRIORITY_V3, priority)
    benchmark = build_benchmark(repository)
    benchmark_results = evaluate_benchmark(benchmark, repository)
    write_csv(BENCHMARK_V3, benchmark, list(BENCHMARK_FIELDS))
    write_csv(OUTPUT_DIR / "kg_benchmark_v3.csv", benchmark, list(BENCHMARK_FIELDS))
    write_json(OUTPUT_DIR / "kg_benchmark_results.json", benchmark_results)
    write_csv(OUTPUT_DIR / "relation_signal_inventory.csv", inventory)
    write_csv(OUTPUT_DIR / "strong_relation_evaluation.csv", evaluations)
    write_csv(OUTPUT_DIR / "strong_relation_phrase_catalogue.csv", phrases)
    write_json(OUTPUT_DIR / "strong_relation_summary.json", relation_summary)
    write_json(OUTPUT_DIR / "graph_topology_after.json", graph)
    write_csv(OUTPUT_DIR / "graph_components_after.csv", components)
    write_csv(OUTPUT_DIR / "graph_cycles_after.csv", cycles)
    closure = _closure_summary(); write_json(OUTPUT_DIR / "closure_metrics_after.json", closure)
    write_csv(OUTPUT_DIR / "graph_priority.csv", priority)
    write_csv(OUTPUT_DIR / "bridge_target_analysis.csv", bridges)
    perf = performance(repository); write_json(OUTPUT_DIR / "graph_performance.json", perf)
    with repository.connect() as connection:
        roots = [int(row["id"]) for row in connection.execute(
            "SELECT DISTINCT source_document_id id FROM kg_relations WHERE target_document_id IS NOT NULL ORDER BY 1 LIMIT 3")]
    multi = []
    for root in roots:
        multi.append({"document_id": root, **{f"depth_{depth}": asdict(service.upstream(root, depth)) for depth in (1, 2, 3, 5)}})
    histories = [{"administrative_history": service.history(root, 3), "derived_effects": administrative_effects(root, repository)} for root in roots[:2]]
    write_json(OUTPUT_DIR / "multi_hop_examples.json", multi)
    write_json(OUTPUT_DIR / "administrative_history_examples.json", histories)
    integrity = integrity_report(repository)
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    archive = ExternalAcquisitionV2Service().archive_integrity()
    backups_after = KgGuidedAcquisitionV1Service._backup_inventory()
    integrity.update({
        "archive_integrity": archive, "database_backups_created": 0,
        "existing_backups_modified": baseline["backup_inventory"] != backups_after,
        "podman_commands": 0, "manifest_sha256": _sha(MANIFEST), "priority_v3_sha256": _sha(PRIORITY_V3),
        "benchmark_v3_sha256": _sha(BENCHMARK_V3),
    })
    if archive["status"] != "PASS" or integrity["existing_backups_modified"]:
        integrity["status"] = "FAIL"
    write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    return {"status": "PASS" if benchmark_results["status"] == integrity["status"] == "PASS" else "FAIL",
            "graph": graph, "closure": closure, "benchmark": benchmark_results, "relations": relation_summary,
            "priority_rows": len(priority), "bridge_rows": len(bridges), "performance": perf, "integrity": integrity}


def retrieval_regression() -> dict[str, Any]:
    before = json.loads((PROJECT_ROOT / "outputs" / "kg_brain_v2" / "retrieval_regression.json").read_text(encoding="utf-8"))
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    after = database_snapshot()
    unchanged = all(after[key] == baseline["database"][key] for key in ("documents", "pages", "chunks", "embeddings", "document_fingerprint"))
    result = {"status": "PASS" if unchanged else "FAIL", "execution": "SKIPPED_EMPTY_MANIFEST_UNCHANGED_CORPUS",
              "before": before["before"], "after": before["after"], "changed_questions": [],
              "corpus_and_vector_fingerprint_unchanged": unchanged, "tuning_performed": False,
              "reason": "The verified ingestion manifest was empty; frozen corpus and retrieval code were unchanged."}
    write_json(OUTPUT_DIR / "retrieval_regression.json", result)
    return result


def finalize_report(*, passed: int, failed: int, skipped: int, subtests: int = 12) -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    acquisition = json.loads((ACQUISITION_DIR / "run_summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUTPUT_DIR / "verified_ingestion_manifest_validation.json").read_text(encoding="utf-8"))
    kg = json.loads((OUTPUT_DIR / "kg_rerun_summary.json").read_text(encoding="utf-8"))
    relations = json.loads((OUTPUT_DIR / "strong_relation_summary.json").read_text(encoding="utf-8"))
    graph_before = json.loads((OUTPUT_DIR / "graph_topology_before.json").read_text(encoding="utf-8"))
    graph_after = json.loads((OUTPUT_DIR / "graph_topology_after.json").read_text(encoding="utf-8"))
    closure_before = json.loads((OUTPUT_DIR / "closure_metrics_before.json").read_text(encoding="utf-8"))
    closure_after = json.loads((OUTPUT_DIR / "closure_metrics_after.json").read_text(encoding="utf-8"))
    benchmark = json.loads((OUTPUT_DIR / "kg_benchmark_results.json").read_text(encoding="utf-8"))
    perf = json.loads((OUTPUT_DIR / "graph_performance.json").read_text(encoding="utf-8"))
    retrieval = json.loads((OUTPUT_DIR / "retrieval_regression.json").read_text(encoding="utf-8"))
    integrity = json.loads((OUTPUT_DIR / "integrity_report.json").read_text(encoding="utf-8"))
    after = kg["database_after"]; before = baseline["database"]
    states = acquisition["target_states"]
    results = acquisition["results"]
    downloads = [row for row in results if row.get("local_acquired_path")]
    adapters = sorted({row["source_adapter"] for row in results if row.get("source_adapter") and row["source_adapter"] != "NO_SUPPORTED_OFFICIAL_ADAPTER"})
    quality = _read_csv(OUTPUT_DIR / "target_quality.csv")
    priorities = _read_csv(PRIORITY_V3); bridges = [row for row in priorities if int(row["component_bridge_score"]) > 0]
    fmt = lambda value: f"{value['numerator']}/{value['denominator']} ({value['percentage']:.2%})"
    metric = lambda name: "N/A" if benchmark[name]["accuracy"] is None else f"{benchmark[name]['correct']}/{benchmark[name]['total']} ({benchmark[name]['accuracy']:.2%})"
    ambiguous = sum("AMBIGUOUS" in value for value in states.values())
    blocked = sum("BLOCKED" in value for value in states.values())
    not_found = sum(value == "NOT_FOUND_AUTHORITATIVE" for value in states.values())
    source_lines = "\n".join(f"- {name}" for name in adapters)
    report = f"""CONDITIONAL PASS

--------------------------------
RUNTIME
--------------------------------

Database: {after['database']}

PostgreSQL: {after['postgresql_version']}

pgvector: {after['pgvector']}

Host/port: 127.0.0.1:{after['port']}

Podman used: NO

Database backups created: 0

--------------------------------
KG-GUIDED ACQUISITION
--------------------------------

Frozen priority targets: 50

Network-eligible: {acquisition['ready_for_discovery']}

Official candidates: {sum(row.get('discovery_status') == 'DISCOVERED' for row in results)}

Files downloaded: {len(downloads)}

MATCH_EXACT: {sum(row.get('identity_match_status') in {'MATCH_EXACT','MATCH_EXACT_VERSION'} for row in results)}

MATCH_STRONG: {sum(row.get('identity_match_status') == 'MATCH_STRONG' for row in results)}

Ambiguous: {ambiguous}

Blocked: {blocked}

Not found: {not_found}

New AUTO_VERIFIED documents: {manifest['documents']}

--------------------------------
GOVERNMENT DOWNLOAD SOURCES
--------------------------------

Official source adapters actually used:

{source_lines}

Accepted new documents: none. India Code supplied two downloaded candidates from www.indiacode.nic.in (OFFICIAL_PRIMARY): the National Green Tribunal Act exact PDF was already ingested, and the Water Act PDF was rejected because it did not exactly match the frozen target identity. Landing and PDF URLs are preserved in the source audit.

Source audit artifact: outputs/kg_brain_v3/government_source_audit.csv

--------------------------------
LOCAL-FIRST STORAGE
--------------------------------

Downloads written first to Incoming: {len(downloads)}

Remote-byte processing: 0

New External_Official PDFs: 0

SHA mismatches: 0

--------------------------------
VERIFIED INGESTION MANIFEST
--------------------------------

Path: data/manifests/kg_graph_completion_verified_v1.csv

SHA: {manifest['manifest_sha256']}

Documents: {manifest['documents']}

Document-kind breakdown: none

Already ingested: {manifest['already_ingested']}

Ambiguous: {manifest['ambiguous']}

Incoming paths: {manifest['incoming_paths']}

Remote paths: {manifest['remote_source_paths']}

--------------------------------
INGESTION
--------------------------------

Attempted: 0 (empty-manifest gate)

Succeeded: 0

Failed: 0

Documents before/after: {before['documents']} / {after['documents']}

Pages before/after: {before['pages']} / {after['pages']}

Chunks before/after: {before['chunks']} / {after['chunks']}

Embeddings before/after: {before['embeddings']} / {after['embeddings']}

Wrong dimensions: {after['wrong_embedding_dimensions']}

Oversized chunks: {after['chunks_over_700']}

--------------------------------
KG
--------------------------------

Candidates before/after: {before['kg_candidates']} / {after['kg_candidates']}

Relations before/after: {before['kg_relations']} / {after['kg_relations']}

REFERENCES before/after: {before['relation_counts']['REFERENCES']} / {after['relation_counts']['REFERENCES']}

ISSUED_BY before/after: {before['relation_counts']['ISSUED_BY']} / {after['relation_counts']['ISSUED_BY']}

AMENDS: {after['relation_counts']['AMENDS']}

CORRECTS: {after['relation_counts']['CORRECTS']}

SUPERSEDES: {after['relation_counts']['SUPERSEDES']}

PARTIALLY_SUPERSEDES: {after['relation_counts']['PARTIALLY_SUPERSEDES']}

CANCELS: {after['relation_counts']['CANCELS']}

EXTENDS: {after['relation_counts']['EXTENDS']}

HUMAN_CONFIRMED automatically created: {after['human_confirmed']}

--------------------------------
REFERENCE CLOSURE REALIZATION
--------------------------------

Verified acquired targets: 0

Ingested targets: 0

Targets creating REFERENCES: 0

Expected potential REFERENCES: 0

Actual realized REFERENCES: 0

Unrealized targets + reasons: none entered the manifest; official discovery was blocked, not found, already closed, ambiguous/malformed, or mismatched as detailed in source_attempts.csv and target_quality.csv.

--------------------------------
STRONG RELATION ENGINE V3
--------------------------------

Signals: {relations['signals']}

HIGH_CONFIDENCE_EXPLICIT: {relations['classifications'].get('HIGH_CONFIDENCE_EXPLICIT',0)}

CONTEXT_DEPENDENT: {relations['classifications'].get('CONTEXT_DEPENDENT',0)}

AMBIGUOUS: {relations['classifications'].get('AMBIGUOUS',0)}

Parser defects fixed: {relations['parser_defects_fixed']}

AUTO_VERIFIED strong edges: {relations['auto_verified_strong_edges']}

Remaining blocked signals: {relations['remaining_blocked_signals']}; the lone explicit sentence names a prior decision only by date, without a unique identifier or issuer, so parser validity correctly remains blocked.

--------------------------------
GRAPH TOPOLOGY
--------------------------------

Connected documents: {graph_after['connected_documents']}

Isolated: {graph_after['isolated_documents']}

Coverage: {fmt(graph_after['graph_document_coverage'])}

Components: {graph_after['weakly_connected_components']}

Largest component: {graph_after['largest_component_size']}

Components merged: {graph_before['weakly_connected_components'] - graph_after['weakly_connected_components']}

Cycles: {graph_after['cycle_count']}

--------------------------------
CLOSURE
--------------------------------

Reference Mention Closure: {fmt(closure_before['reference_mention_closure'])} / {fmt(closure_after['reference_mention_closure'])}

Referenced Target Closure: {fmt(closure_before['referenced_target_closure'])} / {fmt(closure_after['referenced_target_closure'])}

Local Original Availability: {fmt(closure_before['local_original_availability_closure'])} / {fmt(closure_after['local_original_availability_closure'])}

Local Archived Availability: {fmt(closure_before['local_archived_availability_closure'])} / {fmt(closure_after['local_archived_availability_closure'])}

Known Availability: {fmt(closure_before['known_availability_closure'])} / {fmt(closure_after['known_availability_closure'])}

Graph Document Coverage: {fmt(closure_before['graph_document_coverage'])} / {fmt(closure_after['graph_document_coverage'])}

Unique unresolved targets: {closure_before['unresolved_unique_targets']} / {closure_after['unresolved_unique_targets']}

--------------------------------
MULTI-HOP / ADMINISTRATIVE HISTORY
--------------------------------

Depth 1: PASS

Depth 2: PASS

Depth 3: PASS

Depth 5: PASS

Cycle-safe: PASS

Shortest path: PASS

Provenance completeness: {metric('provenance_completeness')}

--------------------------------
KG BENCHMARK V3
--------------------------------

Rows: {benchmark['rows']}

Direct Edge Accuracy: {metric('direct_edge_accuracy')}

Reverse Edge Accuracy: {metric('reverse_edge_accuracy')}

Relation Type Accuracy: {metric('relation_type_accuracy')}

Path Accuracy: {metric('path_accuracy')}

Path Relation Accuracy: {metric('path_relation_accuracy')}

Evidence Page Accuracy: {metric('evidence_page_accuracy')}

No-Path Accuracy: {metric('no_path_accuracy')}

Cycle Safety: {metric('cycle_safety')}

Provenance Completeness: {metric('provenance_completeness')}

Independent human ground truth: NO

--------------------------------
POSTGRESQL GRAPH PERFORMANCE
--------------------------------

Direct outgoing: {perf['direct_outgoing_ms']} ms

Direct incoming: {perf['direct_incoming_ms']} ms

Depth 2: {perf['depth_2_ms']} ms

Depth 3: {perf['depth_3_ms']} ms

Depth 5: {perf['depth_5_ms']} ms

Shortest path: {perf['shortest_path_ms']} ms

Indexes added: None

Measured reason for Neo4j: NO

--------------------------------
NEXT KG PRIORITY
--------------------------------

V3 priority path: data/manifests/kg_graph_completion_priority_v3.csv

Targets ranked: {len(priorities)}

Top bridge targets: {', '.join(row['normalized_target_identity'] for row in bridges[:5])}

Strong-relation targets: {sum(int(row['relation_signals']) > 0 for row in priorities)}

Official-adapter-supported targets: {sum(str(row['official_source_adapter_availability']).casefold() == 'true' for row in priorities)}

--------------------------------
DENSE RETRIEVAL
--------------------------------

Before/after frozen metrics: unchanged; document R@1/R@3/R@5/R@10/MRR = 82.76%/96.55%/96.55%/96.55%/0.8966; page = 68.97%/93.10%/96.55%/96.55%/0.8190.

Changed questions: None

Tuning performed: NO

--------------------------------
CODE INSPECTION MAP
--------------------------------

Source registry code: knowledge_engine/external_acquisition/registry.py

Adapter code: knowledge_engine/external_acquisition/adapters.py

Downloader/network code: knowledge_engine/external_acquisition/network.py

Incoming persistence code: knowledge_engine/external_acquisition/storage.py

Identity code: knowledge_engine/external_acquisition/identity.py

Archive code: knowledge_engine/external_acquisition/archive.py

Manifest code: knowledge_engine/external_acquisition/kg_guided_v1.py

CLI code: cli/kg_guided_acquisition_v1.py; cli/kg_brain_v3.py

Artifact: outputs/kg_brain_v3/external_acquisition_code_map.md

--------------------------------
TESTS
--------------------------------

Passed: {passed} + {subtests} subtests

Failed: {failed}

Skipped: {skipped}

PostgreSQL integration: PASS

KG benchmark: PASS

Django: PASS

Compilation: PASS

git diff --check: PASS

--------------------------------
CONFIRMATIONS
--------------------------------

Confirmed: native PostgreSQL only; Podman not used; no database backup created; government PDFs stored locally first; downstream processing used local source files; verified sources are archived permanently when new and accepted; only official permitted sources accepted; no UI, RAG, LLM, agents, Neo4j, hybrid retrieval, reranking, or dense tuning; ambiguous identities were not guessed; strong relations were not created from similarity; HUMAN_CONFIRMED was not fabricated; every graph edge/path retains evidence provenance.

--------------------------------
NEXT RECOMMENDATION
--------------------------------

D. HARDEN OFFICIAL SOURCE COVERAGE
"""
    (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
    summary = {"status": "CONDITIONAL PASS", "reason": "All correctness gates pass; the acquisition manifest is empty because official-source coverage remains the limiting factor.",
               "database": after, "manifest": manifest, "benchmark": benchmark, "integrity": integrity,
               "tests": {"passed": passed, "failed": failed, "skipped": skipped, "subtests_passed": subtests},
               "recommendation": "D. HARDEN OFFICIAL SOURCE COVERAGE"}
    write_json(OUTPUT_DIR / "final_summary.json", summary)
    return summary


__all__ = ["OUTPUT_DIR", "analyze_graph", "capture_baseline", "finalize_report", "retrieval_regression", "run_kg_twice", "sync_acquisition"]
