from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.evaluation import evaluate_parser
from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.settings import PROJECT_ROOT


def _json_value(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in row.items()}


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_clean_row(row) for row in rows)


def _database_counts(repository: GraphRepository) -> dict[str, int]:
    with repository.connect() as connection:
        row = connection.execute(
            """SELECT (SELECT count(*) FROM documents) documents,
                      (SELECT count(*) FROM pages) pages,
                      (SELECT count(*) FROM chunks) chunks,
                      (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                      (SELECT count(*) FROM gr_relations) legacy_gr_relations,
                      (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
                      (SELECT count(*) FROM kg_relations) kg_relations"""
        ).fetchone()
    return {key: int(value) for key, value in row.items()}


def _graph_metrics(repository: GraphRepository, candidates: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    documents, _ = repository.load_corpus()
    ids = {int(row["id"]) for row in documents}
    reference_edges = [row for row in edges if row["relation_type"] == "REFERENCES"]
    outgoing = Counter(int(row["source_document_id"]) for row in reference_edges)
    incoming = Counter(int(row["target_document_id"]) for row in reference_edges)
    names = {int(row["id"]): row["file_name"] for row in documents}
    parent = {value: value for value in ids}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for edge in reference_edges:
        union(int(edge["source_document_id"]), int(edge["target_document_id"]))
    unresolved_identifier_groups = {
        (row["normalized_document_number"], row["reference_kind"])
        for row in candidates
        if row["resolution_status"] == "NOT_IN_CORPUS" and row["normalized_document_number"]
    }
    unresolved_target_groups = {
        (row["normalized_document_number"], row["reference_kind"], str(row["normalized_reference_date"] or ""))
        for row in candidates
        if row["resolution_status"] == "NOT_IN_CORPUS" and row["normalized_document_number"]
    }
    return {
        **repository.get_graph_summary(),
        "documents_with_outgoing_references": len(outgoing),
        "documents_with_incoming_references": len(incoming),
        "documents_with_zero_outgoing_references": len(ids - set(outgoing)),
        "unique_unresolved_identifier_kind_groups": len(unresolved_identifier_groups),
        "unique_unresolved_target_groups": len(unresolved_target_groups),
        "reference_connected_components_including_isolates": len({find(value) for value in ids}),
        "isolated_documents_in_reference_graph": len(ids - set(outgoing) - set(incoming)),
        "most_cited_documents": [
            {"document_id": document_id, "file_name": names[document_id], "incoming_references": count}
            for document_id, count in incoming.most_common(10)
        ],
        "documents_with_most_outgoing_references": [
            {"document_id": document_id, "file_name": names[document_id], "outgoing_references": count}
            for document_id, count in outgoing.most_common(10)
        ],
        "resolution_statuses": dict(Counter(row["resolution_status"] for row in candidates)),
        "validation_statuses": dict(Counter(row["validation_status"] for row in candidates)),
        "reference_kinds": dict(Counter(row["reference_kind"] for row in candidates)),
        "relation_signals": dict(Counter(row["candidate_relation_type"] for row in candidates if row["candidate_relation_type"])),
        "parser_valid_candidates": sum(row["parser_status"] == "VALID" for row in candidates),
        "self_references_rejected": sum(row["resolution_status"] == "SELF_REFERENCE_REJECTED" for row in candidates),
        "blocked_by_bad_page_quality": sum(bool(row["blocked_by_page_quality"]) for row in candidates),
        "blocked_by_suspicious_target_metadata": sum(bool(row["blocked_by_target_metadata"]) for row in candidates),
        "candidates_requiring_human_review": sum(row["validation_status"] in {"CANDIDATE", "UNRESOLVED"} for row in candidates),
    }


def _integrity(repository: GraphRepository) -> dict[str, Any]:
    with repository.connect() as connection:
        row = connection.execute(
            """SELECT
                (SELECT count(*) FROM documents) documents,
                (SELECT count(*) FROM pages) pages,
                (SELECT count(*) FROM chunks) chunks,
                (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                (SELECT count(*) FROM chunks WHERE token_count>700) chunks_over_700,
                (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) invalid_embedding_dimensions,
                (SELECT coalesce(sum(c-1),0) FROM (SELECT count(*) c FROM documents GROUP BY file_path HAVING count(*)>1) x) duplicate_document_paths,
                (SELECT coalesce(sum(c-1),0) FROM (SELECT count(*) c FROM documents GROUP BY sha256 HAVING count(*)>1) x) duplicate_document_sha,
                (SELECT coalesce(sum(c-1),0) FROM (SELECT count(*) c FROM kg_reference_candidates GROUP BY fingerprint HAVING count(*)>1) x) duplicate_candidate_fingerprints,
                (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.source_document_id WHERE d.id IS NULL) invalid_candidate_source_fks,
                (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.resolved_target_document_id WHERE c.resolved_target_document_id IS NOT NULL AND d.id IS NULL) invalid_candidate_target_fks,
                (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN pages p ON p.document_id=c.source_document_id AND p.page_number=c.source_page WHERE p.id IS NULL) invalid_source_pages,
                (SELECT count(*) FROM kg_relations r WHERE r.relation_type='REFERENCES' AND NOT EXISTS (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) references_without_evidence,
                (SELECT coalesce(sum(c-1),0) FROM (SELECT count(*) c FROM kg_relations GROUP BY source_document_id,target_document_id,target_department_id,relation_type HAVING count(*)>1) x) duplicate_edges,
                (SELECT count(*) FROM kg_relations WHERE relation_type IN ('AMENDS','SUPERSEDES','PARTIALLY_SUPERSEDES','CANCELS','CORRECTS','EXTENDS') AND validation_status<>'HUMAN_CONFIRMED') unconfirmed_strong_relations
            """
        ).fetchone()
    report = {key: int(value) for key, value in row.items()}
    report["status"] = "PASS" if all(report[key] == 0 for key in (
        "chunks_over_700", "invalid_embedding_dimensions", "duplicate_document_paths", "duplicate_document_sha",
        "duplicate_candidate_fingerprints", "invalid_candidate_source_fks", "invalid_candidate_target_fks",
        "invalid_source_pages", "references_without_evidence", "duplicate_edges", "unconfirmed_strong_relations",
    )) else "FAIL"
    return report


def _priorities(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if row["resolution_status"] == "NOT_IN_CORPUS" and row["normalized_document_number"]:
            groups[(row["normalized_document_number"], row["reference_kind"], str(row["normalized_reference_date"] or ""))].append(row)
    rows = []
    for (number, kind, reference_date), values in groups.items():
        sources = sorted({row["source_file"] for row in values})
        departments = Counter(row["source_department"] for row in values)
        complete = int(bool(number and len(number) >= 8))
        known_date = int(bool(reference_date))
        known_department = int(bool(departments))
        high_quality_mentions = sum(not row["blocked_by_page_quality"] for row in values)
        score = len(sources) * 100 + len(values) * 10 + complete * 20 + known_date * 10 + known_department * 5 + high_quality_mentions
        rows.append({
            "normalized_reference_number": number, "reference_kind": kind, "reference_date": reference_date,
            "department": departments.most_common(1)[0][0] if departments else "",
            "distinct_citing_documents": len(sources), "total_mentions": len(values),
            "identifier_complete": "YES" if complete else "NO", "high_quality_mentions": high_quality_mentions,
            "priority_score": score, "example_source_files": ";".join(sources[:5]),
            "priority_reason": f"{len(sources)} citing document(s), {len(values)} mention(s), complete={bool(complete)}, date={bool(known_date)}",
        })
    return sorted(rows, key=lambda row: (-row["priority_score"], row["normalized_reference_number"]))


def _quality_sample(candidates: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    selected = []
    quotas = Counter()
    for row in sorted(candidates, key=lambda value: (value["resolution_status"], value["source_department"], value["reference_kind"], value["pattern_id"], value["id"])):
        key = (row["resolution_status"], row["source_department"], row["reference_kind"], row["source_extraction_method"])
        if quotas[key] >= 1:
            continue
        selected.append(row)
        quotas[key] += 1
        if len(selected) == limit:
            break
    if len(selected) < min(50, len(candidates)):
        used = {row["id"] for row in selected}
        selected.extend(row for row in candidates if row["id"] not in used)
    return selected[:limit]


def run(output_dir: Path, regression_path: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    repository = GraphRepository()
    before = _database_counts(repository)
    evaluation = evaluate_parser(regression_path)
    details = evaluation.pop("details")
    auto_verify = all(evaluation["quality_gate"].values())
    run_result, persistence = KnowledgeGraphPipeline(repository=repository).run(auto_verify_enabled=auto_verify)
    candidates = repository.all_candidates()
    edges = repository.all_edges()
    metrics = _graph_metrics(repository, candidates, edges)
    integrity = _integrity(repository)
    after = _database_counts(repository)

    candidate_fields = [
        "id", "source_document_id", "source_file", "source_department", "source_page", "reference_kind",
        "raw_evidence_text", "raw_document_number", "normalized_document_number", "raw_reference_date",
        "normalized_reference_date", "raw_department", "normalized_department", "pattern_id", "parser_version",
        "parser_status", "parser_notes", "relation_signal", "candidate_relation_type", "evidence_hash", "fingerprint",
        "source_extraction_method", "source_quality_score",
    ]
    resolution_fields = candidate_fields + [
        "resolution_status", "resolved_target_document_id", "target_file", "resolution_method", "resolution_evidence",
        "resolution_candidate_count", "confidence_category", "validation_status", "blocked_by_page_quality",
        "blocked_by_target_metadata",
    ]
    _write_csv(output_dir / "reference_candidates.csv", candidates, candidate_fields)
    _write_csv(output_dir / "reference_resolution.csv", candidates, resolution_fields)
    _write_csv(output_dir / "resolved_references.csv", [row for row in candidates if row["resolution_status"] in {"RESOLVED_EXACT", "RESOLVED_STRONG"}], resolution_fields)
    _write_csv(output_dir / "unresolved_references.csv", [row for row in candidates if row["resolution_status"] in {"NOT_IN_CORPUS", "UNPARSEABLE", "NEEDS_REVIEW"}], resolution_fields)
    _write_csv(output_dir / "ambiguous_references.csv", [row for row in candidates if row["resolution_status"] == "AMBIGUOUS"], resolution_fields)
    _write_csv(output_dir / "relation_signals.csv", [row for row in candidates if row["candidate_relation_type"]], resolution_fields)
    _write_csv(output_dir / "graph_edges.csv", edges)
    review_fields = [
        "id", "source_file", "source_page", "reference_kind", "raw_evidence_text", "raw_document_number",
        "normalized_document_number", "raw_reference_date", "normalized_reference_date", "target_file",
        "resolution_status", "resolution_method", "candidate_relation_type", "validation_status", "review_decision", "review_note",
    ]
    review_rows = [{**row, "review_decision": "", "review_note": ""} for row in candidates]
    _write_csv(output_dir / "review_queue.csv", review_rows, review_fields)
    _write_csv(output_dir / "quality_sample.csv", _quality_sample(candidates), review_fields[:-2])
    priorities = _priorities(candidates)
    _write_csv(output_dir / "unresolved_reference_priority.csv", priorities)
    (output_dir / "graph_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=_json_value) + "\n", encoding="utf-8")
    (output_dir / "integrity_report.json").write_text(json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database_before": before, "database_after": after, "parser_evaluation": evaluation,
        "run": asdict(run_result), "persistence": persistence, "graph_metrics": metrics,
        "integrity": integrity, "auto_verify_enabled": auto_verify,
    }
    (output_dir / "kg_run_summary.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2, default=_json_value) + "\n", encoding="utf-8")
    report = f"""# Knowledge Graph V1 validation report

Generated: {validation['generated_at']}

## Result

The deterministic parser engineering gate {'passed' if auto_verify else 'did not pass'}. This regression set is labelled CODEX_SOURCE_REVIEW and is not independent human ground truth.

- Candidates persisted: {len(candidates)}
- REFERENCES edges: {metrics['references_edges']}
- ISSUED_BY edges: {metrics['issued_by_edges']}
- Integrity: {integrity['status']}
- Detection precision: {evaluation['reference_detection_precision']:.2%}
- Detection recall on labelled examples: {evaluation['reference_detection_recall']:.2%}
- Document-number accuracy: {evaluation['document_number_extraction_accuracy']:.2%}
- Date accuracy: {evaluation['date_extraction_accuracy']:.2%}

No vector similarity or LLM extraction was used. Strong administrative relation signals remain candidates and no authoritative AMENDS/SUPERSEDES/CANCELS/CORRECTS/EXTENDS edges were created automatically.
"""
    (output_dir / "kg_validation_report.md").write_text(report, encoding="utf-8")
    return validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run idempotent Knowledge Graph V1 extraction")
    parser.add_argument("output_dir", type=Path, nargs="?", default=PROJECT_ROOT / "outputs" / "knowledge_graph_v1")
    parser.add_argument("--regression", type=Path, default=PROJECT_ROOT / "data" / "knowledge_graph" / "reference_parser_regression.csv")
    args = parser.parse_args()
    result = run(args.output_dir.resolve(), args.regression.resolve())
    print(json.dumps({
        "run": result["run"], "persistence": result["persistence"],
        "parser_quality_gate": result["parser_evaluation"]["quality_gate"],
        "graph_metrics": result["graph_metrics"], "integrity_status": result["integrity"]["status"],
    }, ensure_ascii=False, indent=2, default=_json_value))


if __name__ == "__main__":
    main()
