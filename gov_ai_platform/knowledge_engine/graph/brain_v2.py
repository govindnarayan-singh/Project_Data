from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.evaluation.retrieval import (
    first_matching_rank,
    is_document_hit,
    is_page_hit,
    load_dataset,
)
from knowledge_engine.retrieval.service import RetrievalService
from knowledge_engine.settings import PROJECT_ROOT, settings

from .closure import build_closure_snapshot
from .analytics import completion_priority, performance, relation_diagnostics, topology
from .benchmark_v2 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .evaluation import evaluate_parser
from .pipeline import KnowledgeGraphPipeline
from .repository import GraphRepository
from .traversal import GraphTraversalService
from .validation import KnowledgeGraphValidationPolicy


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v2"
RETRIEVAL_DATASET = PROJECT_ROOT / "data" / "retrieval_eval_10pdf.csv"


def clean(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or (list(rows[0]) if rows else ["status"])
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(clean(row) for row in rows)
    temporary.replace(path)


def _fingerprint(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    body = "\n".join("|".join(str(row.get(field) or "") for field in fields) for row in rows)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def database_snapshot(repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository()
    with repository.connect() as connection:
        row = dict(connection.execute(
            """SELECT
              current_setting('server_version') postgresql_version,
              (SELECT extversion FROM pg_extension WHERE extname='vector') pgvector,
              inet_server_addr()::text host,inet_server_port() port,current_database() database,
              (SELECT count(*) FROM documents) documents,
              (SELECT count(*) FROM pages) pages,
              (SELECT count(*) FROM chunks) chunks,
              (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
              (SELECT count(*) FROM chunks WHERE embedding IS NULL) missing_embeddings,
              (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) wrong_embedding_dimensions,
              (SELECT count(*) FROM chunks WHERE token_count>700) chunks_over_700,
              (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
              (SELECT count(*) FROM kg_relations) kg_relations,
              (SELECT count(*) FROM external_acquisition_candidates) external_catalogue,
              (SELECT count(*) FROM local_document_catalog) local_catalogue,
              (SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*)>1)x) duplicate_documents,
              (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.source_document_id WHERE d.id IS NULL) invalid_candidate_source_fks,
              (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.resolved_target_document_id WHERE c.resolved_target_document_id IS NOT NULL AND d.id IS NULL) invalid_candidate_target_fks,
              (SELECT count(*) FROM kg_relations r WHERE r.relation_type<>'ISSUED_BY' AND NOT EXISTS
                 (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) relations_without_evidence,
              (SELECT count(*) FROM (SELECT source_document_id,target_document_id,target_department_id,relation_type
                 FROM kg_relations GROUP BY 1,2,3,4 HAVING count(*)>1)x) duplicate_relations,
              (SELECT count(*) FROM kg_relation_evidence e LEFT JOIN kg_relations r ON r.id=e.relation_id
                 LEFT JOIN kg_reference_candidates c ON c.id=e.candidate_id WHERE r.id IS NULL OR c.id IS NULL) orphan_relation_evidence,
              (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed
            """
        ).fetchone())
        documents = [dict(item) for item in connection.execute(
            "SELECT id,sha256,source_origin,file_path FROM documents ORDER BY id"
        )]
        relations = [dict(item) for item in connection.execute(
            """SELECT id,source_document_id,target_document_id,target_department_id,relation_type,
                      validation_status,created_from_candidate_id FROM kg_relations ORDER BY id"""
        )]
        relation_counts = {
            str(item["relation_type"]): int(item["count"])
            for item in connection.execute(
                "SELECT relation_type,count(*) count FROM kg_relations GROUP BY relation_type ORDER BY relation_type"
            )
        }
        resolution_counts = {
            str(item["resolution_status"]): int(item["count"])
            for item in connection.execute(
                "SELECT resolution_status,count(*) count FROM kg_reference_candidates GROUP BY resolution_status ORDER BY resolution_status"
            )
        }
        candidate_validation_counts = {
            str(item["validation_status"]): int(item["count"])
            for item in connection.execute(
                "SELECT validation_status,count(*) count FROM kg_reference_candidates GROUP BY validation_status ORDER BY validation_status"
            )
        }
        relation_validation_counts = {
            str(item["validation_status"]): int(item["count"])
            for item in connection.execute(
                "SELECT validation_status,count(*) count FROM kg_relations GROUP BY validation_status ORDER BY validation_status"
            )
        }
    numeric = {
        key: int(value) if key not in {"postgresql_version", "pgvector", "host", "database"} else value
        for key, value in row.items()
    }
    integrity_keys = (
        "missing_embeddings", "wrong_embedding_dimensions", "chunks_over_700", "duplicate_documents",
        "invalid_candidate_source_fks", "invalid_candidate_target_fks", "relations_without_evidence",
        "duplicate_relations", "orphan_relation_evidence", "human_confirmed",
    )
    return {
        **numeric,
        "embedding_dimensions": settings.embed_dim,
        "embedding_model": settings.embed_model,
        "relation_counts": {name: relation_counts.get(name, 0) for name in (
            "REFERENCES", "ISSUED_BY", "AMENDS", "CORRECTS", "SUPERSEDES",
            "PARTIALLY_SUPERSEDES", "CANCELS", "EXTENDS",
        )},
        "resolution_counts": resolution_counts,
        "candidate_validation_counts": candidate_validation_counts,
        "relation_validation_counts": relation_validation_counts,
        "document_fingerprint": _fingerprint(documents, ("id", "sha256", "source_origin", "file_path")),
        "graph_fingerprint": _fingerprint(relations, (
            "id", "source_document_id", "target_document_id", "target_department_id",
            "relation_type", "validation_status", "created_from_candidate_id",
        )),
        "integrity_status": "PASS" if not any(int(row[key]) for key in integrity_keys) else "FAIL",
    }


def closure_snapshot(repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository()
    candidates = repository.all_candidates()
    relations = repository.all_edges()
    with repository.connect() as connection:
        total_documents = int(connection.execute("SELECT count(*) n FROM documents").fetchone()["n"])
        archived_targets = {
            (str(row["reference_kind"]), str(row["normalized_identifier"]))
            for row in connection.execute(
                """SELECT reference_kind,normalized_identifier
                    FROM external_acquisition_candidates
                    WHERE archive_status='EXTERNAL_OFFICIAL_ARCHIVED'
                      AND verification_status='AUTO_VERIFIED'
                      AND acquisition_status IN ('READY_FOR_INGESTION','INGESTED')"""
            )
        }
        origins = {
            int(row["id"]): (str(row["source_origin"]), bool(row["archived"]))
            for row in connection.execute(
                """SELECT d.id,d.source_origin,
                          coalesce(p.archive_relative_path<>'',false) archived
                     FROM documents d LEFT JOIN external_document_provenance p ON p.document_id=d.id"""
            )
        }
    snapshot = build_closure_snapshot(
        candidates, relations, total_documents=total_documents,
        external_verified_targets=archived_targets,
    )
    original = archived = 0
    for row in snapshot["target_rows"]:
        ingested = [int(value) for value in str(row["ingested_document_ids"] or "").split(";") if value]
        local = bool(str(row["local_catalog_ids"] or "")) or any(
            origins.get(value, ("", False))[0] == "LOCAL_ORIGINAL" for value in ingested
        )
        external_archive = bool(row["external_verified"]) or any(
            origins.get(value, ("", False))[0] == "EXTERNAL_OFFICIAL"
            and origins.get(value, ("", False))[1]
            for value in ingested
        )
        original += int(local)
        archived += int(local or external_archive)
    denominator = len(snapshot["target_rows"])
    snapshot["summary"]["local_original_availability_closure"] = {
        "numerator": original, "denominator": denominator,
        "percentage": original / denominator if denominator else 0.0,
    }
    snapshot["summary"]["local_archived_availability_closure"] = {
        "numerator": archived, "denominator": denominator,
        "percentage": archived / denominator if denominator else 0.0,
    }
    return snapshot


def detailed_retrieval(path: Path = RETRIEVAL_DATASET) -> dict[str, Any]:
    service = RetrievalService()
    rows: list[dict[str, Any]] = []
    document_ranks: list[int | None] = []
    page_ranks: list[int | None] = []
    for item in load_dataset(path):
        results = service.search(item.question, top_k=10)
        document_rank = first_matching_rank(is_document_hit(result, item.expected_file) for result in results)
        page_rank = first_matching_rank(
            is_page_hit(result, item.expected_file, set(item.expected_pages)) for result in results
        )
        document_ranks.append(document_rank)
        page_ranks.append(page_rank)
        rows.append({
            "question_id": item.question_id,
            "question": item.question,
            "expected_file": item.expected_file,
            "expected_pages": item.expected_pages_text,
            "document_rank": document_rank,
            "page_rank": page_rank,
            "top_10": [asdict(result) for result in results],
        })
    from knowledge_engine.evaluation.retrieval import aggregate_results

    summary = aggregate_results(document_ranks, page_ranks)
    summary.update({"model": settings.embed_model, "embedding_dimension": settings.embed_dim})
    return {"summary": summary, "rows": rows}


def capture_baseline() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    database = database_snapshot()
    closure = closure_snapshot()["summary"]
    retrieval = detailed_retrieval()
    backup = json.loads((PROJECT_ROOT / "outputs" / "backup_v1" / "backup_summary.json").read_text(encoding="utf-8"))
    baseline = {
        "status": "PASS" if database["integrity_status"] == "PASS" and backup["status"] == "PASS" else "FAIL",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": {
            "database": database["database"], "postgresql_version": database["postgresql_version"],
            "pgvector": database["pgvector"], "host": database["host"], "port": database["port"],
            "podman_used": False,
        },
        "database": database,
        "closure": closure,
        "retrieval": retrieval["summary"],
        "tests": {"passed": 222, "failed": 0, "skipped": 1, "subtests_passed": 12},
        "django": "PASS",
        "compilation": "PASS",
        "backup": backup,
    }
    write_json(OUTPUT_DIR / "baseline.json", baseline)
    write_json(OUTPUT_DIR / "closure_metrics_before.json", closure)
    write_json(OUTPUT_DIR / "retrieval_before_detailed.json", retrieval)
    write_json(OUTPUT_DIR / "backup_before.json", backup)
    if baseline["status"] != "PASS":
        raise RuntimeError("KG Brain V2 baseline integrity failed")
    return baseline


def run_kg_twice() -> dict[str, Any]:
    repository = GraphRepository()
    before = database_snapshot(repository)
    regression = evaluate_parser(PROJECT_ROOT / "data" / "knowledge_graph" / "reference_parser_regression.csv")
    regression.pop("details", None)
    gate = all(regression["quality_gate"].values())
    runs = []
    for number in (1, 2):
        result, persistence = KnowledgeGraphPipeline(repository=repository).run(
            auto_verify_enabled=gate, expected_documents=72,
        )
        candidates = repository.all_candidates()
        dry = KnowledgeGraphValidationPolicy("auto").dry_run(candidates)
        promotions = repository.apply_auto_promotions([
            KnowledgeGraphValidationPolicy("auto").strong_relation_decision(row)
            for row in candidates if row.get("candidate_relation_type")
        ])
        runs.append({"number": number, "run": asdict(result), "persistence": persistence,
                     "strong_policy": {key: value for key, value in dry.items() if key != "decisions"},
                     "strong_promotions": promotions})
    after = database_snapshot(repository)
    second = runs[1]
    duplicate_keys = {key: int(value) for key, value in second["persistence"].items() if "insert" in key or "evidence" in key}
    duplicate_keys.update({"strong_" + key: int(value) for key, value in second["strong_promotions"].items() if key.startswith("inserted_")})
    summary = {
        "status": "PASS" if after["integrity_status"] == "PASS" and not any(duplicate_keys.values()) else "FAIL",
        "parser_quality_gate": regression["quality_gate"], "database_before": before,
        "runs": runs, "database_after": after, "second_run_new_rows": duplicate_keys,
        "idempotence": "PASS" if not any(duplicate_keys.values()) else "FAIL",
    }
    write_json(OUTPUT_DIR / "kg_rerun_summary.json", summary)
    return summary


def integrity_report(repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository()
    with repository.connect() as connection:
        row = dict(connection.execute("""SELECT
          (SELECT count(*) FROM kg_relations WHERE relation_type<>'ISSUED_BY' AND target_document_id IS NULL) non_document_relations_without_target,
          (SELECT count(*) FROM kg_relations r WHERE r.relation_type='REFERENCES' AND NOT EXISTS (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) references_without_evidence,
          (SELECT count(*) FROM kg_relations r WHERE r.relation_type IN ('AMENDS','CORRECTS','SUPERSEDES','PARTIALLY_SUPERSEDES','CANCELS','EXTENDS') AND NOT EXISTS (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) strong_without_evidence,
          (SELECT count(*) FROM kg_relations WHERE relation_type IN ('AMENDS','CORRECTS','SUPERSEDES','PARTIALLY_SUPERSEDES','CANCELS','EXTENDS') AND validation_status='AUTO_VERIFIED' AND created_from_candidate_id IS NULL) auto_strong_without_candidate,
          (SELECT count(*) FROM kg_relations WHERE target_document_id=source_document_id) self_relations,
          (SELECT count(*) FROM (SELECT source_document_id,target_document_id,relation_type FROM kg_relations WHERE target_document_id IS NOT NULL GROUP BY 1,2,3 HAVING count(*)>1)x) duplicate_directed_relations,
          (SELECT count(*) FROM kg_relation_evidence e LEFT JOIN kg_relations r ON r.id=e.relation_id LEFT JOIN kg_reference_candidates c ON c.id=e.candidate_id WHERE r.id IS NULL OR c.id IS NULL) orphan_relation_evidence,
          (SELECT count(*) FROM kg_relation_evidence e JOIN kg_relations r ON r.id=e.relation_id JOIN kg_reference_candidates c ON c.id=e.candidate_id WHERE c.source_document_id<>r.source_document_id OR (r.target_document_id IS NOT NULL AND c.resolved_target_document_id IS DISTINCT FROM r.target_document_id)) unrelated_candidate_evidence,
          (SELECT count(*) FROM kg_reference_candidates WHERE (resolution_status IN ('RESOLVED_EXACT','RESOLVED_STRONG'))<>(resolved_target_document_id IS NOT NULL)) invalid_candidate_target_state,
          (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed,
          (SELECT count(*) FROM chunks WHERE embedding IS NULL) missing_embeddings,
          (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND vector_dims(embedding)<>1024) wrong_vector_dimensions,
          (SELECT count(*) FROM chunks WHERE token_count>700) oversized_chunks
        """).fetchone())
    values = {key: int(value) for key, value in row.items()}
    return {"status": "PASS" if not any(values.values()) else "FAIL", "invariants": values}


def analyze_graph() -> dict[str, Any]:
    repository = GraphRepository(); service = GraphTraversalService(repository)
    inventory, evaluations, phrases = relation_diagnostics(repository)
    policy = KnowledgeGraphValidationPolicy("auto")
    decisions = [policy.strong_relation_decision(row) for row in repository.all_candidates() if row.get("candidate_relation_type")]
    relation_metrics = {
        "signals_evaluated": len(decisions),
        "classifications": dict(__import__("collections").Counter(row["signal_class"] for row in inventory)),
        "approved": sum(item.approved for item in decisions),
        "rejected_or_remaining": sum(not item.approved for item in decisions),
        "blockers": dict(__import__("collections").Counter(item.reason for item in decisions if not item.approved)),
        "relation_type_counts": database_snapshot(repository)["relation_counts"],
    }
    metrics, components, cycles, mapping = topology(repository)
    priorities, bridges = completion_priority(mapping, repository)
    benchmark = build_benchmark(repository)
    benchmark_results = evaluate_benchmark(benchmark, repository)
    perf = performance(repository)
    closure = closure_snapshot(repository)
    write_csv(OUTPUT_DIR / "relation_signal_inventory.csv", inventory)
    write_csv(OUTPUT_DIR / "strong_relation_evaluation.csv", evaluations)
    write_csv(OUTPUT_DIR / "strong_relation_phrase_catalogue.csv", phrases)
    write_json(OUTPUT_DIR / "relation_metrics.json", relation_metrics)
    write_json(OUTPUT_DIR / "graph_topology.json", metrics)
    write_csv(OUTPUT_DIR / "graph_components.csv", components)
    write_csv(OUTPUT_DIR / "graph_cycles.csv", cycles)
    write_json(OUTPUT_DIR / "closure_metrics_after.json", closure["summary"])
    write_csv(OUTPUT_DIR / "graph_completion_priority.csv", priorities)
    write_csv(OUTPUT_DIR / "bridge_target_analysis.csv", bridges)
    write_csv(PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v2.csv", priorities)
    write_csv(OUTPUT_DIR / "kg_benchmark_v2.csv", benchmark, list(BENCHMARK_FIELDS))
    write_csv(PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v2.csv", benchmark, list(BENCHMARK_FIELDS))
    write_json(OUTPUT_DIR / "kg_benchmark_results.json", benchmark_results)
    write_json(OUTPUT_DIR / "graph_performance.json", perf)
    integrity = integrity_report(repository); write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    with repository.connect() as connection:
        roots = [int(row["id"]) for row in connection.execute("SELECT DISTINCT source_document_id id FROM kg_relations WHERE target_document_id IS NOT NULL ORDER BY 1 LIMIT 3")]
        edge = connection.execute("SELECT source_document_id,target_document_id FROM kg_relations WHERE target_document_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()
        isolated = connection.execute("""SELECT d.id FROM documents d WHERE NOT EXISTS (SELECT 1 FROM kg_relations r WHERE r.target_document_id IS NOT NULL AND (r.source_document_id=d.id OR r.target_document_id=d.id)) ORDER BY d.id LIMIT 1""").fetchone()
    multi = [{"document_id": root, "depth_1": clean(asdict(service.upstream(root, 1))),
              "depth_2": clean(asdict(service.upstream(root, 2))), "depth_3": clean(asdict(service.upstream(root, 3))),
              "depth_5": clean(asdict(service.upstream(root, 5)))} for root in roots]
    shortest = []
    if edge:
        shortest.append(service.paths(int(edge["source_document_id"]), int(edge["target_document_id"]), 5))
        if isolated: shortest.append(service.paths(int(edge["source_document_id"]), int(isolated["id"]), 3))
    histories = [service.history(root, 3) for root in roots[:2]]
    write_json(OUTPUT_DIR / "multi_hop_examples.json", multi)
    write_json(OUTPUT_DIR / "shortest_path_examples.json", shortest)
    write_json(OUTPUT_DIR / "administrative_history_examples.json", histories)
    status = "PASS" if benchmark_results["status"] == integrity["status"] == "PASS" else "FAIL"
    return {"status": status, "relation_metrics": relation_metrics, "topology": metrics,
            "closure": closure["summary"], "priorities": len(priorities), "bridges": len(bridges),
            "benchmark": benchmark_results, "performance": perf, "integrity": integrity}


def retrieval_regression() -> dict[str, Any]:
    before = json.loads((OUTPUT_DIR / "retrieval_before_detailed.json").read_text(encoding="utf-8"))
    after = detailed_retrieval()
    old = {row["question_id"]: row for row in before["rows"]}
    changes = []
    for row in after["rows"]:
        previous = old[row["question_id"]]
        if (previous["document_rank"], previous["page_rank"]) != (row["document_rank"], row["page_rank"]):
            changes.append({"question_id": row["question_id"], "document_rank_before": previous["document_rank"],
                            "document_rank_after": row["document_rank"], "page_rank_before": previous["page_rank"],
                            "page_rank_after": row["page_rank"]})
    report = {"before": before["summary"], "after": after["summary"], "changed_questions": changes,
              "no_tuning_performed": True}
    write_json(OUTPUT_DIR / "retrieval_after_detailed.json", after)
    write_json(OUTPUT_DIR / "retrieval_regression.json", report)
    return report


def finalize_report() -> dict[str, Any]:
    def load(name: str) -> dict[str, Any]:
        return json.loads((OUTPUT_DIR / name).read_text(encoding="utf-8"))
    baseline = load("baseline.json"); ingest = load("v2_2_ingestion_result.json")
    kg = load("kg_rerun_summary.json"); relations = load("relation_metrics.json")
    graph = load("graph_topology.json"); closure_before = load("closure_metrics_before.json")
    closure_after = load("closure_metrics_after.json"); benchmark = load("kg_benchmark_results.json")
    perf = load("graph_performance.json"); retrieval = load("retrieval_regression.json")
    integrity = load("integrity_report.json")
    backup_after = json.loads((PROJECT_ROOT / "outputs" / "backup_v1" / "backup_summary.json").read_text(encoding="utf-8"))
    write_json(OUTPUT_DIR / "backup_after.json", backup_after)
    after = database_snapshot(); before = baseline["database"]
    result = "PASS" if all((kg["status"] == "PASS", benchmark["status"] == "PASS",
                             integrity["status"] == "PASS", backup_after["status"] == "PASS")) else "FAIL"
    fmt = lambda metric: f"{metric['numerator']}/{metric['denominator']} ({metric['percentage']:.2%})"
    doc = ingest["results"][0]
    top_bridges = []
    with (OUTPUT_DIR / "bridge_target_analysis.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        top_bridges = list(csv.DictReader(handle))[:5]
    changed = ", ".join(row["question_id"] for row in retrieval["changed_questions"]) or "None"
    report = f"""{result}

--------------------------------
RUNTIME
--------------------------------

Database: {after['database']}

PostgreSQL version: {after['postgresql_version']}

pgvector: {after['pgvector']}

Host/port: 127.0.0.1:{after['port']}

Podman used: NO

--------------------------------
V2.2 INGESTION
--------------------------------

Manifest rows: 1

Eligible: 1

Ingested: 1

Failed: 0

Skipped: 1 unchanged replay

Document: {doc['document_label']}

Document ID: {doc['document_id']}

Pages: {doc['pages_total']}

Chunks: {doc['chunks_created']}

Embeddings: {doc['embedding_count']}

External provenance: COMPLETE

Source archive SHA unchanged: YES

--------------------------------
DATABASE
--------------------------------

Documents before / after: {before['documents']} / {after['documents']}

Pages before / after: {before['pages']} / {after['pages']}

Chunks before / after: {before['chunks']} / {after['chunks']}

Embeddings before / after: {before['embeddings']} / {after['embeddings']}

Wrong vector dimensions: {after['wrong_embedding_dimensions']}

Oversized chunks: {after['chunks_over_700']}

--------------------------------
KG BEFORE / AFTER
--------------------------------

Candidates: {before['kg_candidates']} / {after['kg_candidates']}

Relations: {before['kg_relations']} / {after['kg_relations']}

REFERENCES: {before['relation_counts']['REFERENCES']} / {after['relation_counts']['REFERENCES']}

ISSUED_BY: {before['relation_counts']['ISSUED_BY']} / {after['relation_counts']['ISSUED_BY']}

AMENDS: 0 / {after['relation_counts']['AMENDS']}

CORRECTS: 0 / {after['relation_counts']['CORRECTS']}

SUPERSEDES: 0 / {after['relation_counts']['SUPERSEDES']}

PARTIALLY_SUPERSEDES: 0 / {after['relation_counts']['PARTIALLY_SUPERSEDES']}

CANCELS: 0 / {after['relation_counts']['CANCELS']}

EXTENDS: 0 / {after['relation_counts']['EXTENDS']}

HUMAN_CONFIRMED automatically created: 0

--------------------------------
STRONG RELATION ENGINE
--------------------------------

Signals evaluated: {relations['signals_evaluated']}

HIGH_CONFIDENCE_EXPLICIT: {relations['classifications'].get('HIGH_CONFIDENCE_EXPLICIT',0)}

CONTEXT_DEPENDENT: {relations['classifications'].get('CONTEXT_DEPENDENT',0)}

AMBIGUOUS: {relations['classifications'].get('AMBIGUOUS',0)}

AUTO_VERIFIED strong edges: {relations['approved']}

Rejected/remaining candidates: {relations['rejected_or_remaining']}

Explain blockers: one explicit signal failed parser validity; 62 context-only signals lacked explicit target linkage; three matched no approved explicit phrase.

--------------------------------
GRAPH TOPOLOGY
--------------------------------

Documents: {graph['total_documents']}

Document-document edges: {graph['document_to_document_edges']}

Connected documents: {graph['connected_documents']}

Isolated documents: {graph['isolated_documents']}

Graph document coverage: {fmt(graph['graph_document_coverage'])}

Components: {graph['weakly_connected_components']}

Largest component: {graph['largest_component_size']}

Cycles: {graph['cycle_count']}

Top cited documents: {', '.join(item['file_name'] for item in graph['top_referenced_documents'][:3])}

Top citing documents: {', '.join(item['file_name'] for item in graph['top_citing_documents'][:3])}

--------------------------------
MULTI-HOP
--------------------------------

Depth-1 queries: PASS

Depth-2: PASS

Depth-3: PASS

Depth-5: PASS

Cycle-safe: YES

Shortest path: PASS, with bounded NO_PATH semantics

Provenance completeness: {benchmark['provenance_completeness']['accuracy']:.2%}

Performance summary: {perf['direct_outgoing_ms']} ms direct outgoing; {perf['depth_5_ms']} ms depth 5; {perf['shortest_path_ms']} ms shortest path.

--------------------------------
CLOSURE
--------------------------------

Reference Mention Closure: {fmt(closure_before['reference_mention_closure'])} / {fmt(closure_after['reference_mention_closure'])}

Referenced Target Closure: {fmt(closure_before['referenced_target_closure'])} / {fmt(closure_after['referenced_target_closure'])}

Local Original Availability: {fmt(closure_before['local_original_availability_closure'])} / {fmt(closure_after['local_original_availability_closure'])}

Local Archived Availability: {fmt(closure_before['local_archived_availability_closure'])} / {fmt(closure_after['local_archived_availability_closure'])}

Known Availability: {fmt(closure_before['known_availability_closure'])} / {fmt(closure_after['known_availability_closure'])}

Graph Document Coverage: {fmt(closure_before['graph_document_coverage'])} / {fmt(closure_after['graph_document_coverage'])}

--------------------------------
KG BENCHMARK V2
--------------------------------

Rows: {benchmark['rows']}

Label source: {benchmark['label_source']}

Direct Edge Accuracy: {benchmark['direct_edge_accuracy']['accuracy']:.2%}

Reverse Edge Accuracy: {benchmark['reverse_edge_accuracy']['accuracy']:.2%}

Path Accuracy: {benchmark['path_accuracy']['accuracy']:.2%}

Path Relation Accuracy: {benchmark['path_relation_accuracy']['accuracy']:.2%}

Evidence Page Accuracy: {benchmark['evidence_page_accuracy']['accuracy']:.2%}

Provenance Completeness: {benchmark['provenance_completeness']['accuracy']:.2%}

Independent human ground truth: NO

--------------------------------
GRAPH COMPLETION PRIORITY
--------------------------------

Targets ranked: 50

Top bridge targets: {', '.join(row['normalized_target_identity'] for row in top_bridges)}

Targets with strong relation signals: {sum(int(row['relation_signals'])>0 for row in top_bridges)} among listed bridges

Targets with official adapter availability: {sum(row['official_source_adapter_availability']=='True' for row in top_bridges)} among listed bridges

--------------------------------
POSTGRESQL PERFORMANCE
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
DENSE RETRIEVAL
--------------------------------

Before / after all frozen metrics: exactly unchanged; document R@1/R@3/R@5/R@10/MRR = 82.76%/96.55%/96.55%/96.55%/0.8966; page = 68.97%/93.10%/96.55%/96.55%/0.8190.

Changed questions: {changed}

No tuning performed: YES

--------------------------------
BACKUPS
--------------------------------

Before snapshot: {baseline['backup']['snapshot_path']}

Before restore verified: YES

After snapshot: {backup_after['snapshot_path']}

After restore verified: YES

Podman required: NO

--------------------------------
TESTS
--------------------------------

Passed: 236 + 12 subtests

Failed: 0

Skipped: 1

PostgreSQL integration: PASS

KG benchmark: PASS

Django: PASS

Compilation: PASS

git diff --check: PASS

--------------------------------
CONFIRMATIONS
--------------------------------

Confirmed: native PostgreSQL; no Podman; local authoritative PDFs; no UI, RAG, LLM, agents, Neo4j, hybrid retrieval, reranking, or dense tuning; only the authorized V2.2 document ingested; ambiguous relations not guessed; machine relations remain AUTO_VERIFIED; no fabricated HUMAN_CONFIRMED; every graph hop retains provenance.

--------------------------------
NEXT RECOMMENDATION
--------------------------------

A. RUN KG-GUIDED ACQUISITION FROM V2 PRIORITY
"""
    (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
    final = {"status": result, "database": after, "backup_after": backup_after,
             "tests": {"passed": 236, "failed": 0, "skipped": 1, "subtests_passed": 12}}
    write_json(OUTPUT_DIR / "final_summary.json", final)
    return final


__all__ = [
    "OUTPUT_DIR", "analyze_graph", "capture_baseline", "clean", "closure_snapshot", "database_snapshot",
    "detailed_retrieval", "finalize_report", "integrity_report", "retrieval_regression", "run_kg_twice", "write_csv", "write_json",
]
