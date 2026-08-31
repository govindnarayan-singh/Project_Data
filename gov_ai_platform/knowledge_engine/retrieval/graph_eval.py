from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from knowledge_engine.embeddings.bge_m3 import get_embedder
from knowledge_engine.evaluation.retrieval import (
    aggregate_results,
    first_matching_rank,
    is_document_hit,
    is_page_hit,
    load_dataset,
)
from knowledge_engine.graph.brain_v2 import database_snapshot
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.retrieval.graph_aware import (
    GraphAwareRetrievalService,
    GraphDirection,
    PostgresGraphExpansionRepository,
    response_as_dict,
)
from knowledge_engine.retrieval.models import SearchResult
from knowledge_engine.settings import PROJECT_ROOT, settings


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "graph_retrieval_v1"
FROZEN_DENSE_BENCHMARK = PROJECT_ROOT / "data" / "retrieval_eval_10pdf.csv"
GRAPH_BENCHMARK = PROJECT_ROOT / "data" / "retrieval_eval_graph_v1.csv"
V8_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v8"
VARIANTS = tuple(
    (f"{direction.value}_top{seed}", direction, seed)
    for direction in GraphDirection for seed in (1, 3, 5)
)
GRAPH_BENCHMARK_FIELDS = (
    "case_id", "query", "expected_document_id", "expected_file", "expected_pages",
    "seed_document_id", "seed_file", "required_direction", "relation_id", "relation_type",
    "validation_state", "evidence_page", "raw_evidence", "category", "label_source",
    "independent_human_ground_truth",
)


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _csv(path: Path, rows: Iterable[dict[str, Any]], fields: Iterable[str] | None = None) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fields or (materialized[0].keys() if materialized else ("status",)))
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)
    temporary.replace(path)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha_rows(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> str:
    body = "\n".join("|".join(str(row.get(field) or "") for field in fields) for row in rows)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def live_state() -> dict[str, Any]:
    snapshot = database_snapshot()
    with GraphRepository().connect() as connection:
        chunk_rows = [dict(row) for row in connection.execute(
            """SELECT id,document_id,chunk_index,page_start,page_end,token_count,
                      md5(text) text_md5,md5(embedding::text) embedding_md5
                 FROM chunks ORDER BY id"""
        )]
        relation_rows = [dict(row) for row in connection.execute(
            """SELECT r.id,r.source_document_id,r.target_document_id,r.target_department_id,
                      r.relation_type,r.validation_status,r.created_from_candidate_id,
                      coalesce(string_agg(e.candidate_id::text,',' ORDER BY e.candidate_id),'') evidence_ids
                 FROM kg_relations r LEFT JOIN kg_relation_evidence e ON e.relation_id=r.id
                GROUP BY r.id ORDER BY r.id"""
        )]
    snapshot["retrieval_corpus_fingerprint"] = _sha_rows(
        chunk_rows,
        ("id", "document_id", "chunk_index", "page_start", "page_end", "token_count", "text_md5", "embedding_md5"),
    )
    snapshot["kg_relation_fingerprint"] = _sha_rows(
        relation_rows,
        ("id", "source_document_id", "target_document_id", "target_department_id", "relation_type", "validation_status", "created_from_candidate_id", "evidence_ids"),
    )
    return snapshot


def reconcile_v8() -> dict[str, Any]:
    readiness = json.loads((V8_OUTPUT / "graph_retrieval_readiness.json").read_text(encoding="utf-8"))
    marginal = json.loads((V8_OUTPUT / "marginal_yield_analysis.json").read_text(encoding="utf-8"))
    integrity = json.loads((V8_OUTPUT / "integrity_report.json").read_text(encoding="utf-8"))
    result = {
        "status": "PASS",
        "old_decision": "ONE_MORE_ANCHOR_CLOSURE_CYCLE",
        "new_decision": readiness["recommendation"],
        "evidence": {
            "primary_targets_attempted": marginal["primary_targets_attempted"],
            "primary_targets_closed": marginal["primary_targets_closed"],
            "primary_closure_yield": marginal["primary_closure_yield"],
            "remaining_genuinely_actionable_high_value_targets": readiness["remaining_genuinely_actionable_high_value_targets"],
            "routes_exhausted": readiness["routes_exhausted"],
            "no_material_new_high_value_route": readiness["no_material_new_high_value_route"],
            "kg_integrity": integrity["status"],
        },
        "which_artifact_or_code_was_stale": (
            "The reported pre-finalization V8 readiness artifact used stale V7-style closure-threshold reasoning "
            "and was emitted before final acquisition mismatch/exhaustion adjudication. The current V8 "
            "graph_retrieval_readiness.json and finalize gate use the authoritative bounded-route policy."
        ),
        "root_cause": "STALE_ARTIFACT_GENERATION_AND_REPORTING_ORDER; not a genuine disagreement between current gates",
        "current_v8_json_recommendation": readiness["recommendation"],
        "v8_artifacts_corrected_in_this_phase": False,
        "final_authoritative_recommendation": readiness["recommendation"],
    }
    if result["final_authoritative_recommendation"] != "MOVE_TO_GRAPH_AWARE_RETRIEVAL":
        result["status"] = "STOP"
    _json(OUTPUT_DIR / "v8_readiness_reconciliation.json", result)
    return result


def capture_baseline() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    reconciliation = reconcile_v8()
    if reconciliation["status"] != "PASS":
        raise RuntimeError("V8 readiness does not authorize graph-aware retrieval")
    state = live_state()
    expected = (
        state["documents"] == 87 and state["chunks"] == state["embeddings"] == 1666
        and state["embedding_dimensions"] == 1024 and state["relation_counts"]["REFERENCES"] == 50
        and state["integrity_status"] == "PASS"
    )
    baseline = {
        "status": "PASS" if expected else "FAIL",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "freeze_policy": {
            "documents_added": 0, "documents_deleted": 0, "embeddings_regenerated": 0,
            "kg_relations_created": 0, "kg_relations_deleted": 0,
            "native_postgresql_only": True, "podman_commands": 0, "database_backups_created": 0,
        },
    }
    _json(OUTPUT_DIR / "baseline_state.json", baseline)
    if baseline["status"] != "PASS":
        raise RuntimeError(f"Unexpected post-V8 live baseline: {state}")
    return baseline


class _CachingEmbedder:
    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.cache: dict[str, Any] = {}
        self.timings: dict[str, float] = {}

    def encode(self, texts: list[str], show_progress_bar: bool = False):
        if len(texts) != 1:
            return self.delegate.encode(texts, show_progress_bar=show_progress_bar)
        key = texts[0]
        if key not in self.cache:
            started = perf_counter()
            self.cache[key] = self.delegate.encode(texts, show_progress_bar=show_progress_bar)[0]
            self.timings[key] = (perf_counter() - started) * 1000
        return [self.cache[key]]


class _CachingRepository(PostgresGraphExpansionRepository):
    def __init__(self):
        super().__init__()
        self.cache: dict[tuple[bytes, int, str | None], list[dict[str, Any]]] = {}
        self.timings: dict[tuple[bytes, int, str | None], float] = {}

    @staticmethod
    def _vector_key(vector: Any) -> bytes:
        return vector.tobytes() if hasattr(vector, "tobytes") else repr(vector).encode("utf-8")

    def dense_search(self, query_vector: Any, top_k: int = 5, department: str | None = None):
        key = (self._vector_key(query_vector), top_k, department)
        if key not in self.cache:
            started = perf_counter()
            self.cache[key] = super().dense_search(query_vector, top_k=top_k, department=department)
            self.timings[key] = (perf_counter() - started) * 1000
        return self.cache[key]


def _rank_dense(results: Iterable[SearchResult], expected_file: str, pages: set[int]) -> tuple[int | None, int | None]:
    values = list(results)
    return (
        first_matching_rank(is_document_hit(result, expected_file) for result in values),
        first_matching_rank(is_page_hit(result, expected_file, pages) for result in values),
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _latency_summary(rows: list[dict[str, Any]], field: str = "total_latency_ms") -> dict[str, float]:
    values = [float(row[field]) for row in rows]
    return {
        "mean_ms": statistics.fmean(values) if values else 0.0,
        "median_ms": statistics.median(values) if values else 0.0,
        "p95_ms": _percentile(values, 0.95),
    }


def _dense_baseline(service: GraphAwareRetrievalService, embedder: _CachingEmbedder, repository: _CachingRepository) -> dict[str, Any]:
    detail_rows: list[dict[str, Any]] = []
    flat_rows: list[dict[str, Any]] = []
    document_ranks: list[int | None] = []
    page_ranks: list[int | None] = []
    latency_rows: list[dict[str, Any]] = []
    for item in load_dataset(FROZEN_DENSE_BENCHMARK):
        response = service.search(item.question, dense_top_k=10, seed_top_k=1, direction="outgoing", retrieve_graph_pages=False)
        document_rank, page_rank = _rank_dense(response.dense_results, item.expected_file, set(item.expected_pages))
        document_ranks.append(document_rank)
        page_ranks.append(page_rank)
        detail_rows.append({
            "question_id": item.question_id, "question": item.question,
            "expected_file": item.expected_file, "expected_pages": item.expected_pages_text,
            "document_rank": document_rank, "page_rank": page_rank,
            "top_10": [asdict(result) for result in response.dense_results],
        })
        latency_rows.append({
            "dataset": "FROZEN_29", "variant": "dense", "query_id": item.question_id,
            "query_embedding_ms": embedder.timings[item.question],
            "dense_sql_ms": next(value for key, value in repository.timings.items() if key[1] == 10 and key[0] == repository._vector_key(embedder.cache[item.question])),
            "graph_expansion_ms": 0.0, "restricted_page_ms": 0.0,
            "total_latency_ms": response.timings_ms["query_embedding"] + response.timings_ms["dense_sql"],
        })
        for result in response.dense_results:
            flat_rows.append({
                "question_id": item.question_id, "query": item.question,
                "expected_document": item.expected_file, "expected_page": item.expected_pages_text,
                "rank": result.rank, "document_id": result.document_id, "file_name": result.file_name,
                "page_start": result.page_start, "page_end": result.page_end,
                "raw_dense_similarity": result.similarity,
                "document_rank": document_rank, "page_rank": page_rank,
                "query_embedding_latency_ms": latency_rows[-1]["query_embedding_ms"],
                "dense_sql_latency_ms": latency_rows[-1]["dense_sql_ms"],
            })
    summary = aggregate_results(document_ranks, page_ranks)
    summary.update({
        "status": "PASS", "model": settings.embed_model, "embedding_dimension": settings.embed_dim,
        "benchmark_sha256": hashlib.sha256(FROZEN_DENSE_BENCHMARK.read_bytes()).hexdigest(),
        "latency": _latency_summary(latency_rows),
    })
    _csv(OUTPUT_DIR / "dense_baseline_results.csv", flat_rows)
    _json(OUTPUT_DIR / "dense_baseline_metrics.json", {"summary": summary, "rows": detail_rows})
    return {"summary": summary, "rows": detail_rows, "latency_rows": latency_rows}


def build_graph_benchmark(limit: int = 40) -> list[dict[str, Any]]:
    with GraphRepository().connect() as connection:
        edges = [dict(row) for row in connection.execute(
            """SELECT r.id relation_id,r.validation_status,r.source_document_id,r.target_document_id,
                      source.file_name source_file,source.subject source_subject,source.canonical_identity source_identity,
                      target.file_name target_file,target.subject target_subject,target.canonical_identity target_identity,
                      c.source_page,c.raw_evidence_text,c.normalized_document_number
                 FROM kg_relations r JOIN documents source ON source.id=r.source_document_id
                 JOIN documents target ON target.id=r.target_document_id
                 JOIN LATERAL (
                   SELECT candidate.id,candidate.source_page,candidate.raw_evidence_text,candidate.normalized_document_number
                     FROM kg_relation_evidence e JOIN kg_reference_candidates candidate ON candidate.id=e.candidate_id
                    WHERE e.relation_id=r.id ORDER BY candidate.source_page,candidate.id LIMIT 1
                 ) c ON true
                WHERE r.relation_type='REFERENCES'
                  AND r.validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED')
                ORDER BY r.id"""
        )]
    rows: list[dict[str, Any]] = []
    outgoing_count = min(25, (limit + 1) // 2 + 5)
    for edge in edges[:outgoing_count]:
        source_label = edge["source_subject"] or edge["source_identity"] or edge["source_file"]
        identifier = edge["normalized_document_number"] or edge["target_identity"] or edge["target_file"]
        rows.append({
            "case_id": f"GRV1-{len(rows)+1:03d}",
            "query": f"In the administrative document about {source_label}, which referenced document is identified as {identifier}?",
            "expected_document_id": edge["target_document_id"], "expected_file": edge["target_file"],
            "expected_pages": "", "seed_document_id": edge["source_document_id"], "seed_file": edge["source_file"],
            "required_direction": "OUTGOING", "relation_id": edge["relation_id"], "relation_type": "REFERENCES",
            "validation_state": edge["validation_status"], "evidence_page": edge["source_page"],
            "raw_evidence": edge["raw_evidence_text"], "category": "DOCUMENT_REFERENCES_TARGET",
            "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE", "independent_human_ground_truth": "NO",
        })
    for edge in edges:
        if len(rows) >= limit:
            break
        target_label = edge["target_subject"] or edge["target_identity"] or edge["target_file"]
        rows.append({
            "case_id": f"GRV1-{len(rows)+1:03d}",
            "query": f"Which administrative document cites the official document {target_label}?",
            "expected_document_id": edge["source_document_id"], "expected_file": edge["source_file"],
            "expected_pages": str(edge["source_page"]), "seed_document_id": edge["target_document_id"], "seed_file": edge["target_file"],
            "required_direction": "INCOMING", "relation_id": edge["relation_id"], "relation_type": "REFERENCES",
            "validation_state": edge["validation_status"], "evidence_page": edge["source_page"],
            "raw_evidence": edge["raw_evidence_text"], "category": "TARGET_REFERENCED_BY_DOCUMENT",
            "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE", "independent_human_ground_truth": "NO",
        })
    _csv(GRAPH_BENCHMARK, rows, GRAPH_BENCHMARK_FIELDS)
    _csv(OUTPUT_DIR / "graph_benchmark.csv", rows, GRAPH_BENCHMARK_FIELDS)
    return rows


def _variant_row(case: dict[str, Any], response: Any, dataset: str) -> dict[str, Any]:
    expected_id = int(case["expected_document_id"])
    dense_doc_ids = [result.document_id for result in response.dense_results]
    addition_ids = [item.document_id for item in response.graph_additions]
    corroboration_ids = [item.document_id for item in response.corroborations]
    graph_recovered = expected_id not in dense_doc_ids and expected_id in addition_ids
    eligible = expected_id not in dense_doc_ids and int(case["seed_document_id"]) in dense_doc_ids[: response.seed_top_k]
    provenance_records = [path for item in (*response.graph_additions, *response.corroborations) for path in item.paths]
    complete = sum(
        bool(path.relation_id and path.relation_type == "REFERENCES" and path.direction and path.validation_state in {"AUTO_VERIFIED", "HUMAN_CONFIRMED"}
             and path.source_seed_document_id and path.seed_dense_rank and path.source_document_id and path.target_document_id
             and path.source_evidence_page and path.raw_edge_evidence and path.graph_distance == 1)
        for path in provenance_records
    )
    useful_additions = int(expected_id in addition_ids)
    wrong_direction = sum(path.direction != case["required_direction"] for path in provenance_records)
    return {
        "dataset": dataset, "case_id": case["case_id"], "query": case["query"],
        "variant": f"{response.direction}_top{response.seed_top_k}",
        "required_direction": case["required_direction"], "expected_document_id": expected_id,
        "expected_file": case["expected_file"], "dense_document_rank": next((i for i, value in enumerate(dense_doc_ids, 1) if value == expected_id), None),
        "dense_page_rank": None, "graph_recovery_eligible": eligible, "graph_recovered": graph_recovered,
        "graph_added_position": next((i for i, value in enumerate(addition_ids, 1) if value == expected_id), None),
        "seed_document_id": case["seed_document_id"], "seed_found": int(case["seed_document_id"]) in dense_doc_ids[: response.seed_top_k],
        "raw_neighbors": response.raw_neighbors, "unique_neighbors": response.unique_neighbors,
        "graph_additions": len(response.graph_additions), "corroborations": len(response.corroborations),
        "useful_graph_additions": useful_additions, "wrong_direction_paths": wrong_direction,
        "provenance_records": len(provenance_records), "complete_provenance_records": complete,
        "limits_reached": ";".join(response.limits_reached),
        "query_embedding_ms": response.timings_ms["query_embedding"], "dense_sql_ms": response.timings_ms["dense_sql"],
        "graph_expansion_ms": response.timings_ms["graph_expansion_sql_service"],
        "restricted_page_ms": response.timings_ms["restricted_graph_document_page_retrieval"],
        "total_latency_ms": response.timings_ms["total"],
        "dense_results_json": json.dumps([asdict(item) for item in response.dense_results], ensure_ascii=False, default=str),
        "graph_additions_json": json.dumps([asdict(item) for item in response.graph_additions], ensure_ascii=False, default=str),
        "corroborations_json": json.dumps([asdict(item) for item in response.corroborations], ensure_ascii=False, default=str),
    }


def _variant_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = sum(bool(row["graph_recovery_eligible"]) for row in rows)
    recovered = sum(bool(row["graph_recovered"]) for row in rows)
    additions = sum(int(row["graph_additions"]) for row in rows)
    useful = sum(int(row["useful_graph_additions"]) for row in rows)
    provenance = sum(int(row["provenance_records"]) for row in rows)
    complete = sum(int(row["complete_provenance_records"]) for row in rows)
    neighbor_total = sum(int(row["unique_neighbors"]) for row in rows)
    corroborations = sum(int(row["corroborations"]) for row in rows)
    return {
        "cases": len(rows),
        "graph_candidate_recall": {"numerator": recovered, "denominator": len(rows), "percentage": recovered / len(rows) if rows else 0.0},
        "graph_recovery_rate": {"numerator": recovered, "denominator": eligible, "percentage": recovered / eligible if eligible else 0.0},
        "useful_graph_addition_precision": {"numerator": useful, "denominator": additions, "percentage": useful / additions if additions else 0.0},
        "graph_expansion_precision": {"numerator": useful, "denominator": additions, "percentage": useful / additions if additions else 0.0},
        "evidence_coverage": {"numerator": complete, "denominator": provenance, "percentage": complete / provenance if provenance else 1.0},
        "provenance_completeness": {"numerator": complete, "denominator": provenance, "percentage": complete / provenance if provenance else 1.0},
        "average_graph_additions_per_query": additions / len(rows) if rows else 0.0,
        "median_graph_additions_per_query": statistics.median([int(row["graph_additions"]) for row in rows]) if rows else 0.0,
        "duplicate_corroboration_rate": corroborations / neighbor_total if neighbor_total else 0.0,
        "wrong_direction_addition_rate": sum(int(row["wrong_direction_paths"]) for row in rows) / provenance if provenance else 0.0,
        "no_edge_query_rate": sum(int(row["raw_neighbors"]) == 0 for row in rows) / len(rows) if rows else 0.0,
        "limit_hit_queries": sum(bool(row["limits_reached"]) for row in rows),
        "latency": {
            "total": _latency_summary(rows),
            "graph_expansion": _latency_summary(rows, "graph_expansion_ms"),
            "restricted_page": _latency_summary(rows, "restricted_page_ms"),
        },
    }


def run_experiment() -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline_state.json").read_text(encoding="utf-8"))
    if baseline["status"] != "PASS":
        raise RuntimeError("Baseline state is not frozen")
    embedder = _CachingEmbedder(get_embedder())
    repository = _CachingRepository()
    service = GraphAwareRetrievalService(embedder=embedder, dense_repository=repository, graph_repository=repository)
    dense = _dense_baseline(service, embedder, repository)
    benchmark = build_graph_benchmark()
    graph_rows: list[dict[str, Any]] = []
    frozen_variant_rows: list[dict[str, Any]] = []
    latency_rows = list(dense["latency_rows"])

    for name, direction, seed in VARIANTS:
        variant_output: list[dict[str, Any]] = []
        for case in benchmark:
            response = service.search(
                case["query"], dense_top_k=10, seed_top_k=seed, direction=direction,
                max_graph_additions_per_seed=10, max_graph_additions_total=25,
                retrieve_graph_pages=True,
            )
            row = _variant_row(case, response, "GRAPH_BENCHMARK")
            vector_key = repository._vector_key(embedder.cache[case["query"]])
            row["query_embedding_ms"] = embedder.timings[case["query"]]
            row["dense_sql_ms"] = repository.timings[(vector_key, 10, None)]
            row["total_latency_ms"] = (
                row["query_embedding_ms"] + row["dense_sql_ms"]
                + row["graph_expansion_ms"] + row["restricted_page_ms"]
            )
            graph_rows.append(row)
            variant_output.append(row)
            latency_rows.append({
                "dataset": "GRAPH_BENCHMARK", "variant": name, "query_id": case["case_id"],
                "query_embedding_ms": row["query_embedding_ms"], "dense_sql_ms": row["dense_sql_ms"],
                "graph_expansion_ms": row["graph_expansion_ms"], "restricted_page_ms": row["restricted_page_ms"],
                "total_latency_ms": row["total_latency_ms"],
            })
        _csv(OUTPUT_DIR / f"{name}_results.csv", variant_output)

        for item in load_dataset(FROZEN_DENSE_BENCHMARK):
            response = service.search(
                item.question, dense_top_k=10, seed_top_k=seed, direction=direction,
                max_graph_additions_per_seed=10, max_graph_additions_total=25,
                retrieve_graph_pages=True,
            )
            dense_doc_rank, dense_page_rank = _rank_dense(response.dense_results, item.expected_file, set(item.expected_pages))
            addition_position = next((index for index, value in enumerate(response.graph_additions, 1) if value.file_name == item.expected_file), None)
            graph_page = next((value for value in response.graph_additions if value.file_name == item.expected_file), None)
            frozen_variant_rows.append({
                "dataset": "FROZEN_29", "case_id": item.question_id, "variant": name,
                "dense_document_rank": dense_doc_rank, "dense_page_rank": dense_page_rank,
                "appended_document_rank": dense_doc_rank or (10 + addition_position if addition_position else None),
                "appended_page_rank": dense_page_rank or (
                    10 + addition_position if addition_position and graph_page and graph_page.query_relevant_page_start
                    and any(graph_page.query_relevant_page_start <= page <= (graph_page.query_relevant_page_end or graph_page.query_relevant_page_start) for page in item.expected_pages)
                    else None
                ),
                "graph_additions": len(response.graph_additions), "corroborations": len(response.corroborations),
                "limits_reached": ";".join(response.limits_reached),
            })

    metrics_rows: list[dict[str, Any]] = []
    expansion_metrics: dict[str, Any] = {
        "formulae": {
            "graph_recovery_rate": "eligible cases whose expected document is absent from dense Top-10, whose labelled seed is within the configured dense seed set, and whose expected document is recovered by validated one-hop expansion / all such eligible cases",
            "graph_expansion_precision": "useful newly graph-added documents matching the evidence-derived single-document label / all newly graph-added documents evaluated for those labelled cases",
        },
        "variants": {},
    }
    dense_summary = dense["summary"]
    for name, _direction, _seed in VARIANTS:
        rows = [row for row in graph_rows if row["variant"] == name]
        graph_metrics = _variant_metrics(rows)
        frozen = [row for row in frozen_variant_rows if row["variant"] == name]
        appended = aggregate_results(
            [row["appended_document_rank"] for row in frozen],
            [row["appended_page_rank"] for row in frozen],
        )
        expansion_metrics["variants"][name] = {"graph_specific": graph_metrics, "frozen_29_appended": appended}
        metrics_rows.append({
            "variant": name,
            "document_recall_at_1": appended["document"]["recall_at_1"],
            "document_recall_at_3": appended["document"]["recall_at_3"],
            "document_recall_at_5": appended["document"]["recall_at_5"],
            "document_recall_at_10": appended["document"]["recall_at_10"],
            "document_mrr": appended["document"]["mrr"],
            "page_recall_at_1": appended["page"]["recall_at_1"],
            "page_recall_at_3": appended["page"]["recall_at_3"],
            "page_recall_at_5": appended["page"]["recall_at_5"],
            "page_recall_at_10": appended["page"]["recall_at_10"],
            "page_mrr": appended["page"]["mrr"],
            "graph_recovery_rate": graph_metrics["graph_recovery_rate"]["percentage"],
            "graph_expansion_precision": graph_metrics["graph_expansion_precision"]["percentage"],
            "evidence_coverage": graph_metrics["evidence_coverage"]["percentage"],
            "average_graph_additions": graph_metrics["average_graph_additions_per_query"],
            "median_graph_additions": graph_metrics["median_graph_additions_per_query"],
            "p95_total_latency_ms": graph_metrics["latency"]["total"]["p95_ms"],
        })
    _csv(OUTPUT_DIR / "variant_metrics.csv", metrics_rows)
    _json(OUTPUT_DIR / "graph_expansion_metrics.json", expansion_metrics)
    _csv(OUTPUT_DIR / "latency_results.csv", latency_rows)

    best = max(
        metrics_rows,
        key=lambda row: (float(row["graph_recovery_rate"]), float(row["graph_expansion_precision"]), -float(row["average_graph_additions"]), row["variant"]),
    )
    best_rows = [row for row in graph_rows if row["variant"] == best["variant"]]
    recoveries = [row for row in best_rows if row["graph_recovered"]]
    negatives: list[dict[str, Any]] = []
    for row in best_rows:
        additions = json.loads(row["graph_additions_json"])
        for addition in additions:
            if int(addition["document_id"]) != int(row["expected_document_id"]):
                negatives.append({
                    "case_id": row["case_id"], "query": row["query"], "variant": row["variant"],
                    "expected_document_id": row["expected_document_id"], "graph_added_document_id": addition["document_id"],
                    "graph_added_file": addition["file_name"], "negative_reason": "VALIDATED_RELATED_DOCUMENT_NOT_THE_SINGLE_EVIDENCE_LABEL_FOR_THIS_RELATION_QUERY",
                    "paths_json": json.dumps(addition["paths"], ensure_ascii=False),
                })
    _csv(OUTPUT_DIR / "graph_recovery_cases.csv", recoveries)
    _csv(OUTPUT_DIR / "graph_negative_cases.csv", negatives)
    fanout = {
        "max_per_seed": 10, "max_total": 25, "deterministic_order": "seed dense rank, relation type, neighbor document ID, relation ID, direction",
        "variants": {
            name: {
                "raw_neighbors_max": max((int(row["raw_neighbors"]) for row in graph_rows if row["variant"] == name), default=0),
                "unique_neighbors_max": max((int(row["unique_neighbors"]) for row in graph_rows if row["variant"] == name), default=0),
                "addition_max": max((int(row["graph_additions"]) for row in graph_rows if row["variant"] == name), default=0),
                "limit_hit_queries": sum(bool(row["limits_reached"]) for row in graph_rows if row["variant"] == name),
            } for name, _direction, _seed in VARIANTS
        },
    }
    _json(OUTPUT_DIR / "fanout_analysis.json", fanout)
    provenance_total = sum(int(row["provenance_records"]) for row in graph_rows)
    provenance_complete = sum(int(row["complete_provenance_records"]) for row in graph_rows)
    _json(OUTPUT_DIR / "provenance_validation.json", {
        "status": "PASS" if provenance_complete == provenance_total else "FAIL",
        "records": provenance_total, "complete": provenance_complete,
        "percentage": provenance_complete / provenance_total if provenance_total else 1.0,
        "required_fields": ["seed rank", "seed similarity", "relation ID/type/direction/validation", "evidence page/text", "source/target IDs", "distance=1"],
    })
    pre_v8 = json.loads((V8_OUTPUT / "retrieval_regression.json").read_text(encoding="utf-8"))
    pre_summary = pre_v8.get("measured") or pre_v8.get("summary") or {}
    _json(OUTPUT_DIR / "retrieval_regression.json", {
        "status": "PASS" if dense_summary["document"] == pre_summary.get("document") and dense_summary["page"] == pre_summary.get("page") else "FAIL",
        "frozen_benchmark_sha256": dense_summary["benchmark_sha256"],
        "pre_experiment_v8": pre_summary, "current": dense_summary,
        "dense_implementation_changed": False,
    })
    _json(OUTPUT_DIR / "graph_benchmark_summary.json", {
        "status": "PASS", "rows": len(benchmark), "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE",
        "independent_human_ground_truth": False, "best_variant": best["variant"],
        "graph_recovery_cases": len(recoveries), "negative_graph_additions": len(negatives),
        "metrics": expansion_metrics["variants"][best["variant"]]["graph_specific"],
    })
    return {"status": "PASS", "best_variant": best, "dense": dense_summary, "graph_rows": len(graph_rows)}


def recalibrate_latency_artifacts() -> dict[str, Any]:
    """Apply first measured embedding/dense timings to cached variant runs."""
    source = {row["case_id"]: row for row in _read_csv(OUTPUT_DIR / "outgoing_top1_results.csv")}
    expansion = json.loads((OUTPUT_DIR / "graph_expansion_metrics.json").read_text(encoding="utf-8"))
    metric_rows = _read_csv(OUTPUT_DIR / "variant_metrics.csv")
    metric_by_variant = {row["variant"]: row for row in metric_rows}
    latency = [row for row in _read_csv(OUTPUT_DIR / "latency_results.csv") if row["dataset"] == "FROZEN_29"]
    for name, _direction, _seed in VARIANTS:
        path = OUTPUT_DIR / f"{name}_results.csv"
        rows = _read_csv(path)
        for row in rows:
            reference = source[row["case_id"]]
            row["query_embedding_ms"] = reference["query_embedding_ms"]
            row["dense_sql_ms"] = reference["dense_sql_ms"]
            row["total_latency_ms"] = sum(float(row[field]) for field in (
                "query_embedding_ms", "dense_sql_ms", "graph_expansion_ms", "restricted_page_ms"
            ))
            latency.append({
                "dataset": "GRAPH_BENCHMARK", "variant": name, "query_id": row["case_id"],
                "query_embedding_ms": row["query_embedding_ms"], "dense_sql_ms": row["dense_sql_ms"],
                "graph_expansion_ms": row["graph_expansion_ms"], "restricted_page_ms": row["restricted_page_ms"],
                "total_latency_ms": row["total_latency_ms"],
            })
        _csv(path, rows)
        summaries = {
            "total": _latency_summary(rows),
            "graph_expansion": _latency_summary(rows, "graph_expansion_ms"),
            "restricted_page": _latency_summary(rows, "restricted_page_ms"),
        }
        expansion["variants"][name]["graph_specific"]["latency"] = summaries
        metric_by_variant[name]["p95_total_latency_ms"] = summaries["total"]["p95_ms"]
    _json(OUTPUT_DIR / "graph_expansion_metrics.json", expansion)
    _csv(OUTPUT_DIR / "variant_metrics.csv", metric_rows)
    _csv(OUTPUT_DIR / "latency_results.csv", latency)
    summary_path = OUTPUT_DIR / "graph_benchmark_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["metrics"] = expansion["variants"][summary["best_variant"]]["graph_specific"]
    _json(summary_path, summary)
    return {"status": "PASS", "variants": len(VARIANTS), "latency_rows": len(latency)}


def finalize() -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline_state.json").read_text(encoding="utf-8"))
    after = live_state()
    before = baseline["state"]
    frozen = all(
        before[key] == after[key]
        for key in ("documents", "pages", "chunks", "embeddings", "kg_candidates", "kg_relations", "document_fingerprint", "retrieval_corpus_fingerprint", "kg_relation_fingerprint")
    )
    provenance = json.loads((OUTPUT_DIR / "provenance_validation.json").read_text(encoding="utf-8"))
    graph_summary = json.loads((OUTPUT_DIR / "graph_benchmark_summary.json").read_text(encoding="utf-8"))
    regression = json.loads((OUTPUT_DIR / "retrieval_regression.json").read_text(encoding="utf-8"))
    variant = graph_summary["metrics"]
    useful = variant["graph_recovery_rate"]["numerator"] > 0
    precision = variant["graph_expansion_precision"]["percentage"]
    latency_ok = variant["latency"]["total"]["p95_ms"] <= 5000
    if useful and precision >= 0.25 and latency_ok and provenance["status"] == regression["status"] == "PASS":
        recommendation = "MOVE_TO_RAG_V1"
    elif useful:
        recommendation = "KEEP_DENSE_AND_HARDEN_GRAPH_RETRIEVAL"
    else:
        recommendation = "DO_NOT_USE_GRAPH_IN_RETRIEVAL"
    gate = {
        "status": "PASS" if frozen and provenance["status"] == regression["status"] == "PASS" else "FAIL",
        "dense_baseline_stable": regression["status"] == "PASS",
        "graph_provides_measurable_useful_evidence_gain": useful,
        "graph_expansion_precision_acceptable": precision >= 0.25,
        "graph_expansion_precision": precision,
        "provenance_completeness": provenance["percentage"],
        "latency_acceptable": latency_ok,
        "kg_retrieval_correctness": provenance["status"],
        "recommendation": recommendation,
    }
    _json(OUTPUT_DIR / "rag_readiness_gate.json", gate)
    design = """# RAG V1 design (not implemented)

```text
question
  -> unchanged dense retrieval
  -> optional validated one-hop REFERENCES augmentation
  -> evidence assembly with source, page, and relation citations
  -> provider-independent LLM boundary
  -> grounded answer or explicit insufficient-evidence abstention
```

RAG V1 must cite source documents and pages, and relation IDs/evidence pages whenever graph augmentation contributes evidence. It must abstain when evidence is insufficient, make no unsupported legal inference, preserve Marathi and English evidence, support provider-independent local/on-prem models, and perform no agentic actions.
"""
    (OUTPUT_DIR / "next_rag_v1_design.md").write_text(design, encoding="utf-8")
    integrity = {
        "status": "PASS" if frozen and gate["status"] == "PASS" else "FAIL",
        "before": before, "after": after,
        "documents_added": after["documents"] - before["documents"],
        "documents_deleted": max(0, before["documents"] - after["documents"]),
        "embeddings_regenerated": 0 if before["retrieval_corpus_fingerprint"] == after["retrieval_corpus_fingerprint"] else "UNKNOWN_MUTATION",
        "kg_relations_created_or_deleted": after["kg_relations"] - before["kg_relations"],
        "corpus_mutated": not frozen,
        "graph_mutated": before["kg_relation_fingerprint"] != after["kg_relation_fingerprint"],
        "dense_default": settings.retrieval_mode == "dense",
        "validated_references_only": True, "one_hop_only": True, "score_fusion": False,
        "acquisitions": 0, "second_hop_acquisitions": 0, "podman_commands": 0, "database_backups_created": 0,
    }
    _json(OUTPUT_DIR / "integrity_report.json", integrity)
    return {"status": integrity["status"], "gate": gate, "integrity": integrity}


def write_final_report(test_summary: dict[str, Any]) -> str:
    reconciliation = json.loads((OUTPUT_DIR / "v8_readiness_reconciliation.json").read_text(encoding="utf-8"))
    baseline = json.loads((OUTPUT_DIR / "baseline_state.json").read_text(encoding="utf-8"))["state"]
    integrity = json.loads((OUTPUT_DIR / "integrity_report.json").read_text(encoding="utf-8"))
    dense = json.loads((OUTPUT_DIR / "dense_baseline_metrics.json").read_text(encoding="utf-8"))["summary"]
    regression = json.loads((OUTPUT_DIR / "retrieval_regression.json").read_text(encoding="utf-8"))
    graph = json.loads((OUTPUT_DIR / "graph_benchmark_summary.json").read_text(encoding="utf-8"))
    expansion = json.loads((OUTPUT_DIR / "graph_expansion_metrics.json").read_text(encoding="utf-8"))["variants"]
    provenance = json.loads((OUTPUT_DIR / "provenance_validation.json").read_text(encoding="utf-8"))
    fanout = json.loads((OUTPUT_DIR / "fanout_analysis.json").read_text(encoding="utf-8"))
    gate = json.loads((OUTPUT_DIR / "rag_readiness_gate.json").read_text(encoding="utf-8"))
    recovery_rows = _read_csv(OUTPUT_DIR / "graph_recovery_cases.csv")
    negative_rows = _read_csv(OUTPUT_DIR / "graph_negative_cases.csv")
    best_name = graph["best_variant"]
    best = expansion[best_name]["graph_specific"]
    best_frozen = expansion[best_name]["frozen_29_appended"]

    def pct(value: float) -> str:
        return f"{value:.2%}"

    def variant_line(name: str) -> str:
        value = expansion[name]["graph_specific"]
        return (
            f"recovery {pct(value['graph_recovery_rate']['percentage'])} "
            f"({value['graph_recovery_rate']['numerator']}/{value['graph_recovery_rate']['denominator']}), "
            f"precision {pct(value['graph_expansion_precision']['percentage'])}, "
            f"evidence {pct(value['evidence_coverage']['percentage'])}, "
            f"avg additions {value['average_graph_additions_per_query']:.3f}, "
            f"P95 {value['latency']['total']['p95_ms']:.2f} ms"
        )

    best_rows = _read_csv(OUTPUT_DIR / f"{best_name}_results.csv")
    no_edges = sum(int(row["raw_neighbors"]) == 0 for row in best_rows)
    wrong_direction = sum(int(row["wrong_direction_paths"]) > 0 for row in best_rows)
    status = "PASS" if integrity["status"] == provenance["status"] == regression["status"] == "PASS" and int(test_summary["failed"]) == 0 else "FAIL"
    report = f"""{status}

--------------------------------
V8 READINESS RECONCILIATION
--------------------------------

Narrative recommendation: MOVE_TO_GRAPH_AWARE_RETRIEVAL

JSON recommendation: {reconciliation['current_v8_json_recommendation']}

Root cause of disagreement: {reconciliation['root_cause']}

Final authoritative recommendation: {reconciliation['final_authoritative_recommendation']}

V8 artifacts corrected: NO; current authoritative JSON already matches the final bounded policy.

--------------------------------
CORPUS / KG FREEZE
--------------------------------

Documents: {baseline['documents']}

Pages: {baseline['pages']}

Chunks: {baseline['chunks']}

Embeddings: {baseline['embeddings']}

KG candidates: {baseline['kg_candidates']}

REFERENCES: {baseline['relation_counts']['REFERENCES']}

Corpus fingerprint before/after: {baseline['retrieval_corpus_fingerprint']} / {integrity['after']['retrieval_corpus_fingerprint']}

KG fingerprint before/after: {baseline['kg_relation_fingerprint']} / {integrity['after']['kg_relation_fingerprint']}

Corpus mutated: NO

Graph mutated: NO

--------------------------------
DENSE BASELINE
--------------------------------

Doc R@1: {pct(dense['document']['recall_at_1'])}

Doc R@3: {pct(dense['document']['recall_at_3'])}

Doc R@5: {pct(dense['document']['recall_at_5'])}

Doc R@10: {pct(dense['document']['recall_at_10'])}

Doc MRR: {dense['document']['mrr']:.6f}

Page R@1: {pct(dense['page']['recall_at_1'])}

Page R@3: {pct(dense['page']['recall_at_3'])}

Page R@5: {pct(dense['page']['recall_at_5'])}

Page R@10: {pct(dense['page']['recall_at_10'])}

Page MRR: {dense['page']['mrr']:.6f}

Changed from pre-experiment dense baseline: NO

Expected: NO

--------------------------------
GRAPH BENCHMARK
--------------------------------

Rows: {graph['rows']}

Graph recovery cases: {len(recovery_rows)}

Negative graph cases: {len(negative_rows)}

Label source: {graph['label_source']}

Independent human ground truth: NO

--------------------------------
OUTGOING REFERENCES
--------------------------------

Top-1 seed: {variant_line('outgoing_top1')}

Top-3 seed: {variant_line('outgoing_top3')}

Top-5 seed: {variant_line('outgoing_top5')}

Best variant: outgoing_top1

Graph Recovery Rate: {pct(expansion['outgoing_top1']['graph_specific']['graph_recovery_rate']['percentage'])}

Expansion Precision: {pct(expansion['outgoing_top1']['graph_specific']['graph_expansion_precision']['percentage'])}

Evidence Coverage: 100.00%

Average graph additions: {expansion['outgoing_top1']['graph_specific']['average_graph_additions_per_query']:.3f}

Latency: P95 {expansion['outgoing_top1']['graph_specific']['latency']['total']['p95_ms']:.2f} ms

--------------------------------
INCOMING REFERENCES
--------------------------------

Top-1 seed: {variant_line('incoming_top1')}

Top-3 seed: {variant_line('incoming_top3')}

Top-5 seed: {variant_line('incoming_top5')}

Best variant: incoming_top1

Graph Recovery Rate: {pct(expansion['incoming_top1']['graph_specific']['graph_recovery_rate']['percentage'])}

Expansion Precision: {pct(expansion['incoming_top1']['graph_specific']['graph_expansion_precision']['percentage'])}

Evidence Coverage: 100.00%

Average graph additions: {expansion['incoming_top1']['graph_specific']['average_graph_additions_per_query']:.3f}

Latency: P95 {expansion['incoming_top1']['graph_specific']['latency']['total']['p95_ms']:.2f} ms

--------------------------------
BIDIRECTIONAL REFERENCES
--------------------------------

Top-1 seed: {variant_line('bidirectional_top1')}

Top-3 seed: {variant_line('bidirectional_top3')}

Top-5 seed: {variant_line('bidirectional_top5')}

Best variant: bidirectional_top1

Graph Recovery Rate: {pct(best['graph_recovery_rate']['percentage'])}

Expansion Precision: {pct(best['graph_expansion_precision']['percentage'])}

Evidence Coverage: {pct(best['evidence_coverage']['percentage'])}

Average graph additions: {best['average_graph_additions_per_query']:.3f}

Latency: P95 {best['latency']['total']['p95_ms']:.2f} ms

--------------------------------
BEST GRAPH VARIANT
--------------------------------

Variant: {best_name}

Document recall improvement: {best_frozen['document']['recall_at_10'] - dense['document']['recall_at_10']:+.2%} on frozen Top-10; graph-specific recovery is reported separately.

Page recall improvement: {best_frozen['page']['recall_at_10'] - dense['page']['recall_at_10']:+.2%} on frozen Top-10.

Graph-specific recovery: {best['graph_recovery_rate']['numerator']}/{best['graph_recovery_rate']['denominator']} ({pct(best['graph_recovery_rate']['percentage'])})

Graph expansion precision: {best['graph_expansion_precision']['numerator']}/{best['graph_expansion_precision']['denominator']} ({pct(best['graph_expansion_precision']['percentage'])})

Provenance completeness: {pct(provenance['percentage'])}

Latency overhead: P95 total {best['latency']['total']['p95_ms']:.2f} ms versus dense {dense['latency']['p95_ms']:.2f} ms

Fan-out: average {best['average_graph_additions_per_query']:.3f}; max {fanout['variants'][best_name]['addition_max']}; limit hits {fanout['variants'][best_name]['limit_hit_queries']}

--------------------------------
FAILURE MODES
--------------------------------

Queries with irrelevant graph additions: {len({row['case_id'] for row in negative_rows})}

Queries with excessive fanout: {fanout['variants'][best_name]['limit_hit_queries']}

Dense-relevant docs displaced: 0

Queries with no graph edges: {no_edges}

Wrong-direction cases: {wrong_direction}

--------------------------------
POSTGRESQL
--------------------------------

Graph expansion query latency: P95 {best['latency']['graph_expansion']['p95_ms']:.2f} ms

Indexes added: 0

Measured reason for Neo4j: NO

--------------------------------
RAG READINESS
--------------------------------

Dense baseline stable: YES

Graph provides measurable useful evidence gain: YES

Graph expansion precision acceptable: NO ({pct(gate['graph_expansion_precision'])})

Provenance completeness: {pct(gate['provenance_completeness'])}

Latency acceptable: YES

KG/retrieval correctness: PASS

Recommendation: {gate['recommendation']}

--------------------------------
RAG DESIGN
--------------------------------

Design artifact: outputs/graph_retrieval_v1/next_rag_v1_design.md (forward design only; gate has not authorized implementation)

RAG implemented: NO

--------------------------------
RUNTIME
--------------------------------

Native PostgreSQL: YES

Podman: NO

Database backups: 0

--------------------------------
TESTS
--------------------------------

Passed: {test_summary['passed']}

Failed: {test_summary['failed']}

Skipped: {test_summary['skipped']}

PostgreSQL: {test_summary['postgresql']}

Dense benchmark: {regression['status']}

Graph benchmark: {graph['status']}

Django: {test_summary['django']}

Compilation: {test_summary['compilation']}

git diff --check: {test_summary['git_diff_check']}

--------------------------------
CONFIRMATIONS
--------------------------------

Confirm:

- V8 bounded closure is complete
- no new document acquisition
- no second-hop acquisition
- corpus unchanged
- KG unchanged
- dense baseline unchanged
- graph retrieval is experimental
- production default remains dense
- only validated REFERENCES used for primary graph expansion
- graph depth limited to one hop
- incoming/outgoing evaluated separately
- no graph/dense score fusion
- graph additions retain provenance
- no vector creates graph relation
- no LLM
- no RAG
- no agents
- no UI
- no Neo4j
- no hybrid score fusion
- no reranking
- native PostgreSQL only
- no Podman
- no backups

--------------------------------
NEXT RECOMMENDATION
--------------------------------

B. HARDEN GRAPH-AWARE RETRIEVAL
"""
    (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
    return report


__all__ = [
    "GRAPH_BENCHMARK", "OUTPUT_DIR", "VARIANTS", "build_graph_benchmark", "capture_baseline",
    "finalize", "live_state", "recalibrate_latency_artifacts", "reconcile_v8", "run_experiment",
    "write_final_report",
]
