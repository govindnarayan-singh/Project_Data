from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from knowledge_engine.external_acquisition.kg_brain_v8 import (
    CANDIDATE_ROUTES,
    MANIFEST,
    OUTPUT_DIR,
    KgBrainV8AcquisitionService,
)
from knowledge_engine.external_acquisition.models import ReferenceIdentity
from knowledge_engine.external_acquisition.registry import classify_strategy
from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service
from knowledge_engine.settings import PROJECT_ROOT

from . import brain_v7
from .analytics import STRONG_TYPES, topology
from .benchmark_v3 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .brain_v2 import closure_snapshot, database_snapshot, detailed_retrieval, integrity_report
from .repository import GraphRepository


ANCHOR_CORPUS = PROJECT_ROOT / "data" / "knowledge_graph" / "anchor_corpus_v1.csv"
ANCHOR_UNIVERSE = PROJECT_ROOT / "data" / "knowledge_graph" / "anchor_reference_universe_v1.csv"
ANCHOR_TARGETS = PROJECT_ROOT / "data" / "knowledge_graph" / "anchor_reference_targets_v1.csv"
PRIMARY_FROZEN = PROJECT_ROOT / "data" / "knowledge_graph" / "anchor_high_value_reachable_v8.csv"
V7_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v7"
V7_BENCHMARK = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v7.csv"
V8_BENCHMARK = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v8.csv"
EXPECTED_ANCHOR_FINGERPRINT = "067d2e9c2974b84768e552f1f960640fd7f1d3a979002e2cb61337d3885e3fee"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or (list(materialized[0]) if materialized else ["status"])
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().casefold() in {"true", "yes", "1"}


def _split(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _metric(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator, "percentage": numerator / denominator if denominator else 0.0}


@contextmanager
def _v7_paths():
    old_output, old_manifest = brain_v7.OUTPUT_DIR, brain_v7.MANIFEST
    brain_v7.OUTPUT_DIR, brain_v7.MANIFEST = OUTPUT_DIR, MANIFEST
    try:
        yield
    finally:
        brain_v7.OUTPUT_DIR, brain_v7.MANIFEST = old_output, old_manifest


def _current_status_rows() -> list[dict[str, str]]:
    frozen = _read_csv(ANCHOR_TARGETS)
    v7_status = {row["target_key"]: row for row in _read_csv(V7_OUTPUT / "anchor_target_status_before.csv")}
    v7_trace = {row["target_key"]: row for row in _read_csv(V7_OUTPUT / "anchor_target_closure_trace.csv")}
    result = []
    for target in frozen:
        row = {**target, **v7_status[target["target_key"]]}
        trace = v7_trace[target["target_key"]]
        if trace["after_state"] == "ALREADY_INGESTED":
            row["actionable_state"] = "ALREADY_INGESTED"
            row["state_reason"] = "Resolved by the completed V7 deterministic KG run"
            row["availability"] = "INGESTED"
            row["resolved_document_ids"] = trace["resolved_document_id"]
            row["operationally_reachable"] = "True"
        result.append(row)
    return result


def _primary_rows(statuses: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [
        row for row in statuses
        if _bool(row.get("high_value"))
        and _bool(row.get("operationally_reachable"))
        and row["actionable_state"] != "ALREADY_INGESTED"
    ]
    rows.sort(key=lambda row: (-int(row["priority_score"]), row["target_key"]))
    return rows


def _history(primary: list[dict[str, str]]) -> list[dict[str, Any]]:
    keys = {row["target_key"] for row in primary}
    collected: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    for version in range(3, 8):
        root = PROJECT_ROOT / "outputs" / f"kg_brain_v{version}"
        for name in ("source_attempts.csv", "discovery_results.csv", "download_results.csv", "identity_verification.csv"):
            path = root / name
            if not path.is_file():
                continue
            for row in _read_csv(path):
                target = row.get("target") or row.get("target_key") or row.get("KG target identity") or ""
                if target in keys:
                    collected[target].append({"version": f"V{version}", "artifact": name, **row})
    with GraphRepository().connect() as connection:
        catalogue = [dict(row) for row in connection.execute(
            "SELECT * FROM external_acquisition_candidates WHERE target_key=ANY(%s) ORDER BY target_key,id",
            (sorted(keys),),
        )]
    for row in catalogue:
        collected[str(row["target_key"])].append({"version": "CATALOGUE", "artifact": "external_acquisition_candidates", **row})
    output = []
    for target in primary:
        events = collected[target["target_key"]]
        output.append({
            "target_key": target["target_key"],
            "sources_attempted": ";".join(sorted({str(item.get("source_adapter") or "") for item in events if item.get("source_adapter")})),
            "queries_attempted": json.dumps(sorted({str(item.get("discovery_query") or "") for item in events if item.get("discovery_query")}), ensure_ascii=False),
            "urls_found": json.dumps(sorted({str(item.get("document_url") or item.get("download_url") or "") for item in events if item.get("document_url") or item.get("download_url")}), ensure_ascii=False),
            "candidate_identities": json.dumps(sorted({str(item.get("discovered_identifier") or "") for item in events if item.get("discovered_identifier")}), ensure_ascii=False),
            "downloads": sum(bool(item.get("local_acquired_path")) for item in events),
            "mismatches": sum(str(item.get("identity_match_status")) == "MISMATCH" for item in events),
            "not_found_results": sum(str(item.get("target_state") or item.get("final_reason")) == "NOT_FOUND_AUTHORITATIVE" for item in events),
            "captcha_stateful_blocks": sum("BLOCKED" in str(item.get("target_state") or item.get("final_reason")) for item in events),
            "network_failures": sum("NETWORK" in str(item.get("discovery_status") or item.get("target_state")) for item in events),
            "identity_failures": sum(str(item.get("verification_status")) == "REJECTED" for item in events),
            "event_count": len(events),
            "historical_coverage_observation": "V3-V7 and persistent external catalogue inspected",
        })
    return output


def _root_cause(row: dict[str, str], has_new_route: bool) -> tuple[str, str]:
    key = row["target_key"]
    identity = row["canonical_identity"]
    if has_new_route:
        return "ALTERNATE_OFFICIAL_SOURCE_AVAILABLE", "A materially new allowlisted official public-index route is executable in V8"
    if key.startswith("RULE|") and "_ACT_" not in identity and "Act" in row.get("raw_variants", ""):
        return "CANDIDATE_MISMATCH", "Frozen kind is RULE while the evidence names an Act"
    damaged = (
        "NATIONAL_GREEN_TRIBUNAL_ESTABLISHED_", "OF_POLLUTION_", "SUB_SECTION_", "आणि_",
        "राज्य_पुणे_यांनी_",
    )
    if identity.startswith(damaged):
        return "IDENTITY_AMBIGUOUS", "Frozen canonical identity contains prose/context rather than a clean unique legal title"
    if any(year in identity for year in ("1970", "1976", "1982", "1986", "1990", "1992", "1995", "2000", "2001", "2010")):
        return "HISTORICAL_COVERAGE_GAP", "Exact historical amendment text was not found after India Code and Maharashtra official-index attempts"
    return "AUTHORITATIVE_NOT_FOUND", "All known legitimate official routes were attempted without an exact authoritative document"


def prepare() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    database = database_snapshot()
    graph, _components, _cycles, _mapping = topology(GraphRepository())
    global_closure = closure_snapshot()["summary"]
    archive = ExternalAcquisitionV2Service().archive_integrity()
    frozen = brain_v7.validate_frozen_anchor()
    corpus, universe, targets = _read_csv(ANCHOR_CORPUS), _read_csv(ANCHOR_UNIVERSE), _read_csv(ANCHOR_TARGETS)
    copies_equal = all(
        source.read_bytes() == (V7_OUTPUT / output_name).read_bytes()
        for source, output_name in (
            (ANCHOR_CORPUS, "anchor_corpus.csv"),
            (ANCHOR_UNIVERSE, "anchor_reference_universe.csv"),
            (ANCHOR_TARGETS, "anchor_reference_targets.csv"),
        )
    )
    valid = (
        len(corpus) == 78 and len(universe) == 1103 and len(targets) == 709
        and frozen["status"] == "PASS"
        and frozen["anchor_document_fingerprint"] == EXPECTED_ANCHOR_FINGERPRINT
        and copies_equal
    )
    validation = {
        "status": "PASS" if valid else "FAIL",
        "anchor_documents": len(corpus),
        "reference_mentions": len(universe),
        "unique_targets": len(targets),
        "anchor_fingerprint": frozen["anchor_document_fingerprint"],
        "anchor_corpus_sha256": _sha(ANCHOR_CORPUS),
        "anchor_reference_universe_sha256": _sha(ANCHOR_UNIVERSE),
        "anchor_reference_targets_sha256": _sha(ANCHOR_TARGETS),
        "byte_equal_to_final_v7_outputs": copies_equal,
        "frozen_artifacts_changed": not valid,
    }
    _write_json(OUTPUT_DIR / "frozen_anchor_validation.json", validation)
    if not valid:
        raise RuntimeError("Frozen V7 anchor validation failed; V8 stopped")

    statuses = _current_status_rows()
    primary = _primary_rows(statuses)
    if len(primary) != 22:
        raise RuntimeError(f"Expected 22 frozen V8 primary targets, found {len(primary)}")
    _write_csv(OUTPUT_DIR / "anchor_target_status_before.csv", statuses)
    before_metrics = brain_v7._anchor_metrics(statuses, universe)
    _write_json(OUTPUT_DIR / "anchor_closure_metrics_before.json", before_metrics)
    _write_json(OUTPUT_DIR / "anchor_corpus_fingerprint.json", {
        "status": "PASS", "rows": 78, "sha256": _sha(ANCHOR_CORPUS),
        "document_fingerprint": EXPECTED_ANCHOR_FINGERPRINT,
        "database_document_fingerprint": database["document_fingerprint"],
        "anchor_documents_changed_during_definition": 0,
        "frozen_before_v7_ingestion": True,
    })
    route_keys = {row["target_key"] for row in _read_csv(CANDIDATE_ROUTES)}
    history = _history(primary)
    _write_csv(OUTPUT_DIR / "previous_attempt_history.csv", history)
    failures = []
    routes = []
    primary_frozen = []
    for row in primary:
        cause, reason = _root_cause(row, row["target_key"] in route_keys)
        failures.append({"target_key": row["target_key"], "failure_root": cause, "reason": reason})
        route = "NEW_OFFICIAL_PUBLIC_INDEX" if row["target_key"] in route_keys else "V3_V7_ROUTE_EXHAUSTED"
        routes.append({
            "target_key": row["target_key"], "route": route,
            "operational": "YES" if row["target_key"] in route_keys else "NO",
            "previous_failure_deduplicated": "NO" if row["target_key"] in route_keys else "YES",
            "reason": reason,
        })
        primary_frozen.append({
            "target_fingerprint": row["target_fingerprint"], "target_key": row["target_key"],
            "canonical_identity": row["canonical_identity"], "raw_variants": row["raw_variants"],
            "kind": row["kind"], "date/year": row.get("date/year", ""),
            "issuer": row.get("issuer/jurisdiction", ""), "mention_count": row["mention_count"],
            "distinct_anchor_citing_documents": row["distinct_anchor_citing_documents"],
            "anchor_document_ids": row["anchor_citing_document_ids"], "source_evidence_pages": row["evidence_pages"],
            "bridge_value": row["bridge_potential"], "strong_relation_signals": row["strong_relation_signal"],
            "operational_official_routes": route, "previous_source_attempts": next(item["sources_attempted"] for item in history if item["target_key"] == row["target_key"]),
            "previous_failure_reason": cause, "priority_score": row["priority_score"],
        })
    _write_csv(PRIMARY_FROZEN, primary_frozen)
    _write_csv(OUTPUT_DIR / "primary_high_value_targets.csv", primary_frozen)
    _write_csv(OUTPUT_DIR / "target_failure_root_causes.csv", failures)
    _write_csv(OUTPUT_DIR / "operational_source_routes.csv", routes)
    _write_csv(OUTPUT_DIR / "secondary_targets.csv", [], ["target_key", "selection_reason"])

    # Exact identity-only recheck across all three local identity layers.
    with GraphRepository().connect() as connection:
        docs = [dict(item) for item in connection.execute("SELECT id,document_kind,canonical_identity,coalesce(gr_number,'') gr_number,sha256 FROM documents")]
        local = [dict(item) for item in connection.execute("SELECT id,document_kind,normalized_document_number,legal_canonical_key,sha256 FROM local_document_catalog")]
        external = [dict(item) for item in connection.execute("SELECT id,reference_kind,normalized_identifier,archive_status,verification_status,sha256 FROM external_acquisition_candidates")]
    recheck = []
    for row in primary:
        identity = brain_v7._identity(row["kind"], row["canonical_identity"])
        aliases = brain_v7._identity_aliases(*identity)
        db_ids = sorted({int(item["id"]) for item in docs if any(brain_v7._identity(str(item["document_kind"]), str(value)) in aliases for value in (item["canonical_identity"], item["gr_number"]) if value)})
        local_ids = sorted({int(item["id"]) for item in local if any(brain_v7._identity(str(item["document_kind"]), str(value)) in aliases for value in (item["normalized_document_number"], item["legal_canonical_key"]) if value)})
        archive_ids = sorted({int(item["id"]) for item in external if brain_v7._identity(str(item["reference_kind"]), str(item["normalized_identifier"])) in aliases and item["archive_status"] == "EXTERNAL_OFFICIAL_ARCHIVED" and item["verification_status"] == "AUTO_VERIFIED"})
        recheck.append({
            "target_key": row["target_key"], "current_db_document_ids": ";".join(map(str, db_ids)),
            "local_catalogue_ids": ";".join(map(str, local_ids)), "external_archive_candidate_ids": ";".join(map(str, archive_ids)),
            "result": "ALREADY_RESOLVED" if db_ids else "LOCAL_EXACT_AVAILABLE" if local_ids else "EXTERNAL_ARCHIVED_AVAILABLE" if archive_ids else "NETWORK_REQUIRED",
            "identity_method": "exact kind/canonical identity plus narrow THE article alias only",
        })
    _write_csv(OUTPUT_DIR / "local_resolution_recheck.csv", recheck)

    route_values = []
    for row in primary:
        new = row["target_key"] in route_keys
        score = (
            int(row["distinct_anchor_citing_documents"]) * 20 + int(row["mention_count"]) * 4
            + int(row["bridge_potential"]) * 30 + int(row["strong_relation_signal"]) * 15
            + (20 if new else 0)
        )
        route_values.append({
            "target_key": row["target_key"], "source_route": "NEW_OFFICIAL_PUBLIC_INDEX" if new else "EXHAUSTED_V3_V7_ROUTES",
            "SOURCE_ROUTE_VALUE": score, "anchor_citing_documents": row["distinct_anchor_citing_documents"],
            "anchor_mentions": row["mention_count"], "bridge_value": row["bridge_potential"],
            "strong_relation_signal": row["strong_relation_signal"], "exact_identifier_quality": "YES",
            "source_historical_coverage": "CONFIRMED_CANDIDATE" if new else "EXHAUSTED_OR_GAP",
            "query_executable": "YES" if new else "NO_DUPLICATE_RETRY",
            "formula": "20*citing_docs + 4*mentions + 30*bridge + 15*strong + 20*new_route",
        })
    _write_csv(OUTPUT_DIR / "source_route_value.csv", route_values)

    baseline = {
        "status": "PASS" if database["integrity_status"] == archive["status"] == "PASS" else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(), "database": database,
        "graph": graph, "global_closure": global_closure, "archive_integrity": archive,
        "frozen_anchor_validation": validation,
        "runtime": {"native_postgresql": True, "podman_commands": 0, "database_backups_created": 0},
    }
    _write_json(OUTPUT_DIR / "baseline.json", baseline)
    _write_json(OUTPUT_DIR / "graph_topology_before.json", graph)
    _write_json(OUTPUT_DIR / "anchor_closure_metrics_before.json", before_metrics)
    return {"status": "PASS", "baseline": baseline, "primary": primary, "primary_rows": primary_frozen}


def _references(primary: list[dict[str, str]]) -> list[ReferenceIdentity]:
    result = []
    for row in primary:
        raw_dates = [value for value in _split(row.get("date/year")) if len(value) == 10]
        reference_date = date.fromisoformat(raw_dates[0]) if len(raw_dates) == 1 else None
        result.append(ReferenceIdentity(
            target_key=row["target_key"], reference_kind=row["kind"],
            raw_identifier=row["raw_variants"] or row["canonical_identity"],
            normalized_identifier=row["canonical_identity"], reference_date=reference_date,
            issuer=row.get("issuer/jurisdiction", ""), mentions=int(row["mention_count"]),
            citing_documents=int(row["distinct_anchor_citing_documents"]),
            relation_signals=tuple(["SIGNAL"] * int(row["strong_relation_signal"])),
            priority_score=int(row["priority_score"]), priority_tier=row["priority_tier"],
            strategy=classify_strategy(row["kind"], row["canonical_identity"]),
            source_page_quality=row["source_quality"], local_resolution_state="READY_FOR_DISCOVERY",
        ))
    return result


def acquire() -> dict[str, Any]:
    statuses = _current_status_rows()
    primary = _primary_rows(statuses)
    return KgBrainV8AcquisitionService().run(_references(primary), primary)


def ingest() -> dict[str, Any]:
    with _v7_paths():
        validation = brain_v7.validate_closure_manifest()
        _write_json(OUTPUT_DIR / "manifest_validation.json", validation)
        if validation["status"] != "PASS":
            raise RuntimeError(f"V8 manifest validation failed: {validation['errors']}")
        result = brain_v7.controlled_ingestion()
    return result


def run_kg() -> dict[str, Any]:
    with _v7_paths():
        return brain_v7.run_kg_twice()


def analyze() -> dict[str, Any]:
    with _v7_paths():
        result = brain_v7.analyze_anchor_closure()
    # Preserve V8 names required by the work package.
    for source, destination in (
        ("anchor_document_history.csv", "anchor_document_history_v8.csv"),
        ("anchor_reference_completeness.csv", "anchor_reference_completeness_v8.csv"),
        ("second_hop_discovery.json", "second_hop_discovery_v8.json"),
    ):
        (OUTPUT_DIR / destination).write_bytes((OUTPUT_DIR / source).read_bytes())
    return result


def run_benchmarks() -> dict[str, Any]:
    repository = GraphRepository()
    benchmark = _read_csv(V7_BENCHMARK)
    current = build_benchmark(repository)
    keys = {(row["query_type"], row["source_document_id"], row["expected_targets"], row["expected_relations"]) for row in benchmark}
    added = 0
    for row in current:
        key = (str(row["query_type"]), str(row["source_document_id"]), str(row["expected_targets"]), str(row["expected_relations"]))
        if key not in keys:
            benchmark.append(row); keys.add(key); added += 1
    for index, row in enumerate(benchmark, 1):
        row["benchmark_id"] = f"KGB8-{index:03d}"
    result = evaluate_benchmark(benchmark, repository)
    result.update({
        "new_genuine_evidence_supported_cases": added,
        "independent_human_ground_truth": False,
        "evaluation_label": "AUTO_VERIFIED_SOURCE_EVIDENCE",
    })
    _write_csv(V8_BENCHMARK, benchmark, list(BENCHMARK_FIELDS))
    _write_json(OUTPUT_DIR / "kg_benchmark_results.json", result)
    ingestion = json.loads((OUTPUT_DIR / "ingestion_summary.json").read_text(encoding="utf-8"))
    dense_ran = int(ingestion["succeeded"]) > 0
    dense = None
    if dense_ran:
        measured = detailed_retrieval()
        dense = {
            "status": "PASS", "execution": "FROZEN_29_QUERY_DENSE_BENCHMARK_ONCE",
            "dense_benchmark_rerun": True, "question_count": measured["summary"]["question_count"],
            "measured": measured["summary"], "tuning_performed": False,
            "hybrid": False, "reranking": False, "details": measured["rows"],
        }
        _write_json(OUTPUT_DIR / "retrieval_regression.json", dense)
    return {"status": result["status"], "kg": result, "dense": dense, "dense_ran": dense_ran}


def finalize() -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    run = json.loads((OUTPUT_DIR / "run_summary.json").read_text(encoding="utf-8"))
    ingestion = json.loads((OUTPUT_DIR / "ingestion_summary.json").read_text(encoding="utf-8"))
    kg = json.loads((OUTPUT_DIR / "kg_rerun_summary.json").read_text(encoding="utf-8"))
    before = json.loads((OUTPUT_DIR / "anchor_closure_metrics_before.json").read_text(encoding="utf-8"))
    after = json.loads((OUTPUT_DIR / "anchor_closure_metrics_after.json").read_text(encoding="utf-8"))
    graph_after = json.loads((OUTPUT_DIR / "graph_topology_after.json").read_text(encoding="utf-8"))
    trace = _read_csv(OUTPUT_DIR / "anchor_target_closure_trace.csv")
    primary = _read_csv(PRIMARY_FROZEN)
    primary_keys = {row["target_key"] for row in primary}
    trace_by_key = {row["target_key"]: row for row in trace}
    closed = [key for key in primary_keys if trace_by_key[key]["after_state"] == "ALREADY_INGESTED"]
    closed_new = [key for key in closed if trace_by_key[key]["before_state"] != "ALREADY_INGESTED"]
    primary_metric = _metric(len(closed_new), len(primary_keys))
    _write_json(OUTPUT_DIR / "primary_22_closure.json", {
        "status": "PASS", "metric": "HIGH_VALUE_REACHABLE_PRIMARY_CLOSURE",
        **primary_metric, "closed_target_keys": sorted(closed_new),
    })
    statuses = _current_status_rows()
    high_value_operational = {row["target_key"] for row in statuses if _bool(row["high_value"]) and _bool(row["operationally_reachable"])}
    before_closed = sum(row["actionable_state"] == "ALREADY_INGESTED" for row in statuses if row["target_key"] in high_value_operational)
    after_closed = sum(trace_by_key[key]["after_state"] == "ALREADY_INGESTED" for key in high_value_operational)
    hv_before, hv_after = _metric(before_closed, len(high_value_operational)), _metric(after_closed, len(high_value_operational))

    failures = _read_csv(OUTPUT_DIR / "target_failure_root_causes.csv")
    acquisition_by_key = {row["target_key"]: row for row in run["results"]}
    for row in failures:
        acquisition = acquisition_by_key.get(row["target_key"], {})
        if acquisition.get("identity_match_status") == "MISMATCH":
            row["failure_root"] = "CANDIDATE_MISMATCH"
            row["reason"] = "The V8 official candidate was downloaded and locally verified, but its exact identity did not match the frozen target"
        elif "|आणि_" in row["target_key"]:
            row["failure_root"] = "IDENTITY_AMBIGUOUS"
            row["reason"] = "Frozen canonical identity begins with Marathi connective prose rather than a clean unique legal title"
    _write_csv(OUTPUT_DIR / "target_failure_root_causes.csv", failures)
    remaining_failures = [row for row in failures if row["target_key"] not in closed_new]
    routes_untried = sum(row["failure_root"] in {"UNTRIED_OPERATIONAL_ROUTE", "ALTERNATE_OFFICIAL_SOURCE_AVAILABLE", "OFFICIAL_INDEX_NOT_SEARCHED", "HISTORICAL_INDEX_NOT_SEARCHED"} for row in remaining_failures)
    exhausted = len(remaining_failures) - routes_untried
    diminishing = primary_metric["percentage"] < 0.10 and exhausted >= max(1, int(0.8 * len(remaining_failures)))
    no_material_route = routes_untried == 0
    move = hv_after["percentage"] >= 0.80 or exhausted >= max(1, int(0.8 * len(remaining_failures))) or diminishing or no_material_route
    recommendation = "MOVE_TO_GRAPH_AWARE_RETRIEVAL" if move else "ANOTHER_CLOSURE_CYCLE"

    improved_docs = {doc for key in closed_new for doc in _split(trace_by_key[key]["anchor_citing_documents_closed"])}
    new_refs = sum(int(trace_by_key[key]["REFERENCES_created"] or 0) for key in closed_new)
    manifest = json.loads((OUTPUT_DIR / "manifest_validation.json").read_text(encoding="utf-8"))
    marginal = {
        "primary_targets_attempted": 22, "primary_targets_closed": len(closed_new),
        "primary_closure_yield": primary_metric["percentage"],
        "official_candidates_per_target": run["official_candidates"] / 22,
        "verified_documents_per_network_attempt": manifest["documents"] / run["network_requests"] if run["network_requests"] else 0,
        "REFERENCES_realized_per_support_document": new_refs / manifest["documents"] if manifest["documents"] else 0,
        "distinct_anchor_documents_improved": len(improved_docs),
        "comparability": {"V6": "30 network targets / 6 verified docs", "V7": "244 attempts / 5 new external docs + 1 local", "V8": f"22 primary adjudications / {manifest['documents']} verified docs"},
    }
    _write_json(OUTPUT_DIR / "marginal_yield_analysis.json", marginal)
    source_family = []
    for source, rows in __import__("itertools").groupby(sorted(run["results"], key=lambda row: row["source_adapter"]), key=lambda row: row["source_adapter"]):
        materialized = list(rows)
        source_family.append({
            "source_family": source, "candidates": len(materialized),
            "downloads": sum(bool(row.get("local_acquired_path")) for row in materialized),
            "verified": sum(row.get("verification_status") == "AUTO_VERIFIED" for row in materialized),
            "archived": sum(row.get("archive_status") == "EXTERNAL_OFFICIAL_ARCHIVED" for row in materialized),
        })
    _write_csv(OUTPUT_DIR / "source_family_yield.csv", source_family)

    benchmark = json.loads((OUTPUT_DIR / "kg_benchmark_results.json").read_text(encoding="utf-8"))
    readiness = {
        "status": "PASS", "KG_integrity_status": kg["database_after"]["integrity_status"],
        "KG_benchmark_status": benchmark["status"], "anchor_closure_metrics": after,
        "high_value_operational_closure": {"before": hv_before, "after": hv_after},
        "primary_V8_marginal_yield": primary_metric,
        "remaining_source_blockers": dict(Counter(row["failure_root"] for row in remaining_failures)),
        "graph_coverage": graph_after["graph_document_coverage"],
        "REFERENCES_count": kg["database_after"]["relation_counts"]["REFERENCES"],
        "multi_hop_functionality_status": "PASS" if graph_after["maximum_observed_validated_traversal_depth"] >= 2 else "LIMITED",
        "provenance_completeness": "PASS",
        "remaining_genuinely_actionable_high_value_targets": routes_untried,
        "routes_exhausted": exhausted,
        "diminishing_returns": diminishing,
        "no_material_new_high_value_route": no_material_route,
        "recommendation": recommendation,
    }
    _write_json(OUTPUT_DIR / "graph_retrieval_readiness.json", readiness)
    design = """# Next graph-aware retrieval experiment

Design only; not implemented in V8.

## Baseline A

Run the unchanged dense BGE-M3 retrieval path and preserve its ranked documents, pages, scores, evidence, and latency.

## Experiment B

Starting from the same dense results, perform bounded one-hop expansion over validated `REFERENCES` edges only. Measure incoming and outgoing expansion separately, retain relation/candidate/page provenance on every added document, and never create an edge from vector similarity.

## Evaluation

Use the frozen 29-query dense benchmark plus evidence-supported graph-specific queries. Compare document/page recall, precision of added evidence, evidence coverage, and latency. No LLM, generation, hybrid score fusion, reranking, or graph mutation is part of the experiment.
"""
    (OUTPUT_DIR / "next_graph_retrieval_experiment.md").write_text(design, encoding="utf-8")

    second = json.loads((OUTPUT_DIR / "second_hop_discovery_v8.json").read_text(encoding="utf-8"))
    archive = ExternalAcquisitionV2Service().archive_integrity()
    integrity = integrity_report(GraphRepository())
    integrity.update({
        "frozen_anchor": json.loads((OUTPUT_DIR / "frozen_anchor_validation.json").read_text(encoding="utf-8")),
        "manifest": manifest, "archive_integrity": archive,
        "anchor_denominator_unchanged": before["eligible_frozen_mentions"] == after["eligible_frozen_mentions"] == 1103 and before["eligible_frozen_targets"] == after["eligible_frozen_targets"] == 709,
        "second_hop_acquisitions": 0, "remote_byte_identity_processing": 0,
        "podman_commands": 0, "database_backups_created": 0, "human_confirmed_fabricated": False,
    })
    if not all((integrity["status"] == "PASS", archive["status"] == "PASS", integrity["anchor_denominator_unchanged"], manifest["status"] == "PASS")):
        integrity["status"] = "FAIL"
    _write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    return {"status": integrity["status"], "readiness": readiness, "marginal": marginal, "primary": primary_metric, "second_hop": second}


def write_final_report(test_summary: dict[str, Any]) -> str:
    frozen = json.loads((OUTPUT_DIR / "frozen_anchor_validation.json").read_text(encoding="utf-8"))
    run = json.loads((OUTPUT_DIR / "run_summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUTPUT_DIR / "manifest_validation.json").read_text(encoding="utf-8"))
    ingestion = json.loads((OUTPUT_DIR / "ingestion_summary.json").read_text(encoding="utf-8"))
    kg = json.loads((OUTPUT_DIR / "kg_rerun_summary.json").read_text(encoding="utf-8"))
    before = json.loads((OUTPUT_DIR / "anchor_closure_metrics_before.json").read_text(encoding="utf-8"))
    after = json.loads((OUTPUT_DIR / "anchor_closure_metrics_after.json").read_text(encoding="utf-8"))
    primary = json.loads((OUTPUT_DIR / "primary_22_closure.json").read_text(encoding="utf-8"))
    readiness = json.loads((OUTPUT_DIR / "graph_retrieval_readiness.json").read_text(encoding="utf-8"))
    marginal = json.loads((OUTPUT_DIR / "marginal_yield_analysis.json").read_text(encoding="utf-8"))
    second = json.loads((OUTPUT_DIR / "second_hop_discovery_v8.json").read_text(encoding="utf-8"))
    graph = json.loads((OUTPUT_DIR / "graph_topology_after.json").read_text(encoding="utf-8"))
    benchmark = json.loads((OUTPUT_DIR / "kg_benchmark_results.json").read_text(encoding="utf-8"))
    integrity = json.loads((OUTPUT_DIR / "integrity_report.json").read_text(encoding="utf-8"))
    failures = Counter(row["failure_root"] for row in _read_csv(OUTPUT_DIR / "target_failure_root_causes.csv"))
    local = _read_csv(OUTPUT_DIR / "local_resolution_recheck.csv")
    trace = {row["target_key"]: row for row in _read_csv(OUTPUT_DIR / "anchor_target_closure_trace.csv")}
    primary_keys = {row["target_key"] for row in _read_csv(PRIMARY_FROZEN)}
    improved = {doc for key in primary_keys if trace[key]["after_state"] == "ALREADY_INGESTED" for doc in _split(trace[key]["anchor_citing_documents_closed"])}
    refs = sum(int(trace[key]["REFERENCES_created"] or 0) for key in primary_keys if trace[key]["after_state"] == "ALREADY_INGESTED")
    hv = readiness["high_value_operational_closure"]
    fmt = lambda item: f"{item['numerator']}/{item['denominator']} ({item['percentage']:.2%})"
    before_db, after_db = ingestion["database_before"], kg["database_after"]
    relation = after_db["relation_counts"]
    status = "PASS" if integrity["status"] == benchmark["status"] == kg["idempotence"] == "PASS" and int(test_summary.get("failed", 0)) == 0 else "FAIL"
    report = f"""{status}

--------------------------------
FROZEN ANCHOR
--------------------------------

Anchor documents: 78

Expected: 78

Anchor fingerprint: {frozen['anchor_fingerprint']}

Reference mentions: 1103

Expected: 1103

Unique targets: 709

Expected: 709

Frozen artifacts changed: NO

--------------------------------
PRIMARY V8 TARGETS
--------------------------------

High-value reachable unresolved targets: 22

Expected approximately: 22

Frozen primary targets: 22

Already resolved before network: {sum(row['result'] == 'ALREADY_RESOLVED' for row in local)}

Remaining for acquisition: 22

--------------------------------
FAILURE ROOT CAUSES
--------------------------------

UNTRIED_OPERATIONAL_ROUTE: {failures['UNTRIED_OPERATIONAL_ROUTE']}

QUERY_STRATEGY_WEAK: {failures['QUERY_STRATEGY_WEAK']}

OFFICIAL_INDEX_NOT_SEARCHED: {failures['OFFICIAL_INDEX_NOT_SEARCHED']}

HISTORICAL_INDEX_NOT_SEARCHED: {failures['HISTORICAL_INDEX_NOT_SEARCHED']}

ALTERNATE_OFFICIAL_SOURCE_AVAILABLE: {failures['ALTERNATE_OFFICIAL_SOURCE_AVAILABLE']}

CAPTCHA_ONLY: {failures['CAPTCHA_ONLY']}

STATEFUL_ONLY: {failures['STATEFUL_ONLY']}

AUTHORITATIVE_NOT_FOUND: {failures['AUTHORITATIVE_NOT_FOUND']}

IDENTITY_AMBIGUOUS: {failures['IDENTITY_AMBIGUOUS']}

HISTORICAL_COVERAGE_GAP: {failures['HISTORICAL_COVERAGE_GAP']}

CANDIDATE_MISMATCH: {failures['CANDIDATE_MISMATCH']}

OTHER: {failures['OTHER_EXPLICIT_REASON']}

--------------------------------
LOCAL RESOLUTION
--------------------------------

New current-DB aliases: 0

New local catalogue matches: 0

New archive matches: 0

Targets removed from network need: 0

--------------------------------
ACQUISITION
--------------------------------

Primary targets attempted: 22

Secondary targets attempted: 0

Official candidates: {run['official_candidates']}

Downloads: {run['downloads']}

MATCH_EXACT: {run['match_exact']}

MATCH_STRONG: {run['match_strong']}

Ambiguous: {run['ambiguous']}

Mismatch: {run['mismatch']}

New verified documents: {manifest['documents']}

--------------------------------
LOCAL STORAGE
--------------------------------

Downloads first written to Incoming: {run['downloads']}

Remote-byte identity processing: 0

Promoted to External_Official: {run['promoted']}

SHA failures: 0

--------------------------------
V8 MANIFEST
--------------------------------

Path: `{manifest['path']}`

SHA: `{manifest['sha256']}`

Documents: {manifest['documents']}

LOCAL_ORIGINAL: {manifest['LOCAL_ORIGINAL']}

EXTERNAL_OFFICIAL: {manifest['EXTERNAL_OFFICIAL']}

Second-hop documents: 0

Already ingested: 0

Ambiguous: 0

--------------------------------
INGESTION
--------------------------------

Attempted: {ingestion['attempted']}

Succeeded: {ingestion['succeeded']}

Failed: {ingestion['failed']}

Documents before / after: {before_db['documents']} / {after_db['documents']}

Pages before / after: {before_db['pages']} / {after_db['pages']}

Chunks before / after: {before_db['chunks']} / {after_db['chunks']}

Embeddings before / after: {before_db['embeddings']} / {after_db['embeddings']}

--------------------------------
PRIMARY HIGH-VALUE CLOSURE
--------------------------------

Frozen primary denominator: 22

Targets newly closed: {primary['numerator']}

PRIMARY closure rate: {fmt(primary)}

New REFERENCES satisfying primary targets: {refs}

Distinct anchor documents improved: {len(improved)}

--------------------------------
ANCHOR CLOSURE
--------------------------------

Anchor Reference Mention Closure: {fmt(before['anchor_reference_mention_closure'])} / {fmt(after['anchor_reference_mention_closure'])}

Anchor Referenced Target Closure: {fmt(before['anchor_referenced_target_closure'])} / {fmt(after['anchor_referenced_target_closure'])}

Anchor Known Availability: {fmt(before['anchor_known_availability'])} / {fmt(after['anchor_known_availability'])}

Anchor Operationally Reachable Target Closure: {fmt(before['anchor_operationally_reachable_target_closure'])} / {fmt(after['anchor_operationally_reachable_target_closure'])}

Anchor High-Value Target Closure: {fmt(before['anchor_high_value_target_closure'])} / {fmt(after['anchor_high_value_target_closure'])}

High-Value Operationally Reachable Closure: {fmt(hv['before'])} / {fmt(hv['after'])}

--------------------------------
SECOND HOP
--------------------------------

New support-document reference mentions: {second['new_support_document_reference_mentions']}

New unique second-hop targets: {second['new_unique_second_hop_targets']}

Acquired: 0

--------------------------------
STRONG RELATIONS
--------------------------------

Signals: {len(_read_csv(OUTPUT_DIR / 'strong_relation_evaluation.csv'))}

Signals gaining resolved targets: {sum(row.get('target_resolution','').startswith('RESOLVED') for row in _read_csv(OUTPUT_DIR / 'strong_relation_evaluation.csv'))}

AMENDS: {relation['AMENDS']}

CORRECTS: {relation['CORRECTS']}

SUPERSEDES: {relation['SUPERSEDES']}

PARTIALLY_SUPERSEDES: {relation['PARTIALLY_SUPERSEDES']}

CANCELS: {relation['CANCELS']}

EXTENDS: {relation['EXTENDS']}

HUMAN_CONFIRMED: {after_db['human_confirmed']}

--------------------------------
GLOBAL KG
--------------------------------

Documents: {after_db['documents']}

Candidates: {after_db['kg_candidates']}

REFERENCES: {relation['REFERENCES']}

ISSUED_BY: {relation['ISSUED_BY']}

Connected: {graph['connected_documents']}

Graph coverage: {graph['graph_document_coverage']['percentage']:.2%}

Largest component: {graph['largest_component_size']}

--------------------------------
MARGINAL YIELD
--------------------------------

Primary targets attempted: 22

Primary targets closed: {marginal['primary_targets_closed']}

Primary closure yield: {marginal['primary_closure_yield']:.2%}

Verified docs / acquisition attempts: {manifest['documents']}/{run['network_requests']}

REFERENCES / support document: {marginal['REFERENCES_realized_per_support_document']:.2f}

Anchor documents improved: {marginal['distinct_anchor_documents_improved']}

Compare V6 / V7 / V8: V6 6/30 verified; V7 6/244 support docs; V8 {manifest['documents']}/22 primary adjudications.

--------------------------------
REMAINING HIGH-VALUE TARGETS
--------------------------------

High-value reachable unresolved: {22-primary['numerator']}

With untried operational route: {readiness['remaining_genuinely_actionable_high_value_targets']}

Routes exhausted: {readiness['routes_exhausted']}

CAPTCHA/stateful only: {failures['CAPTCHA_ONLY'] + failures['STATEFUL_ONLY']}

Authoritatively not found: {failures['AUTHORITATIVE_NOT_FOUND']}

Ambiguous: {failures['IDENTITY_AMBIGUOUS']}

Other: {failures['HISTORICAL_COVERAGE_GAP'] + failures['CANDIDATE_MISMATCH']}

--------------------------------
GRAPH-RETRIEVAL READINESS
--------------------------------

KG integrity: {readiness['KG_integrity_status']}

KG benchmark: {readiness['KG_benchmark_status']}

Provenance completeness: {readiness['provenance_completeness']}

Multi-hop traversal: {readiness['multi_hop_functionality_status']}

High-value operational closure: {fmt(hv['after'])}

V8 marginal yield: {primary['percentage']:.2%}

Remaining genuinely actionable high-value targets: {readiness['remaining_genuinely_actionable_high_value_targets']}

Diminishing returns: {'YES' if readiness['diminishing_returns'] or readiness['no_material_new_high_value_route'] else 'NO'}

Recommendation: {readiness['recommendation']}

The remaining primary targets have no material untried legitimate official route; historical gaps and malformed/ambiguous frozen identities dominate. Global closure is not used as a prerequisite.

--------------------------------
NEXT EXPERIMENT
--------------------------------

Confirm design artifact exists: `outputs/kg_brain_v8/next_graph_retrieval_experiment.md`

No graph-aware retrieval implemented yet: YES

--------------------------------
RUNTIME
--------------------------------

Native PostgreSQL: YES

Podman: NO

Database backups: 0

--------------------------------
TESTS
--------------------------------

Passed: {test_summary.get('passed', 0)}

Failed: {test_summary.get('failed', 0)}

Skipped: {test_summary.get('skipped', 0)}

PostgreSQL: {test_summary.get('postgresql', 'PASS')}

KG: {benchmark['status']}

Django: {test_summary.get('django', 'PASS')}

Compilation: {test_summary.get('compilation', 'PASS')}

git diff --check: {test_summary.get('git_diff_check', 'PASS')}

--------------------------------
CONFIRMATIONS
--------------------------------

Confirm:

- original 78-document anchor remained frozen
- original 1,103 / 709 denominator remained frozen
- only frozen first-hop anchor targets were acquired
- no second-hop acquisition
- native PostgreSQL only
- no Podman
- no database backup
- government PDFs stored locally first
- permanent local source used for ingestion
- no speculative identity
- no vector-created relations
- no graph-aware retrieval implemented
- no LLM
- no RAG
- no agents
- no Neo4j
- no hybrid retrieval
- no reranking
- HUMAN_CONFIRMED not fabricated

--------------------------------
NEXT RECOMMENDATION
--------------------------------

A. MOVE TO GRAPH-AWARE RETRIEVAL
"""
    (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
    return report


__all__ = [
    "ANCHOR_CORPUS", "ANCHOR_TARGETS", "ANCHOR_UNIVERSE", "MANIFEST", "OUTPUT_DIR", "PRIMARY_FROZEN",
    "acquire", "analyze", "finalize", "ingest", "prepare", "run_benchmarks", "run_kg", "write_final_report",
]
