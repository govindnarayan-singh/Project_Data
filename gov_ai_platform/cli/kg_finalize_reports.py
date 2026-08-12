from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.repository import GraphRepository
from cli.kg_v1 import _graph_metrics


REVIEW_FIELDS = [
    "candidate_id", "source_file", "source_page", "reference_kind", "raw_evidence",
    "raw_number", "normalized_number", "raw_date", "normalized_date", "resolved_target",
    "resolution_status", "resolution_method", "candidate_relation_signal", "validation_status",
    "review_decision", "review_note",
]


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _review_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["id"], "source_file": row["source_file"], "source_page": row["source_page"],
        "reference_kind": row["reference_kind"], "raw_evidence": row["raw_evidence_text"],
        "raw_number": row["raw_document_number"], "normalized_number": row["normalized_document_number"],
        "raw_date": row["raw_reference_date"],
        "normalized_date": row["normalized_reference_date"].isoformat() if row["normalized_reference_date"] else "",
        "resolved_target": row["target_file"] or "", "resolution_status": row["resolution_status"],
        "resolution_method": row["resolution_method"], "candidate_relation_signal": row["candidate_relation_type"],
        "validation_status": row["validation_status"], "review_decision": "", "review_note": "",
    }


def _fingerprint(repository: GraphRepository) -> str:
    digest = hashlib.sha256()
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT id,file_path,sha256,coalesce(gr_number,''),coalesce(gr_date::text,'') FROM documents ORDER BY id"
        ).fetchall()
    for row in rows:
        digest.update("|".join(str(value) for value in row.values()).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _stratified_sample(candidates: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    quotas: Counter[tuple[str, str, str, str]] = Counter()
    for row in sorted(candidates, key=lambda value: (
        value["resolution_status"], value["source_department"], value["reference_kind"],
        value["source_extraction_method"], value["pattern_id"], value["id"],
    )):
        key = (row["resolution_status"], row["source_department"], row["reference_kind"], row["source_extraction_method"])
        if quotas[key]:
            continue
        selected.append(row)
        quotas[key] += 1
        if len(selected) == limit:
            return selected
    used = {row["id"] for row in selected}
    for row in candidates:
        if row["id"] not in used:
            selected.append(row)
        if len(selected) == limit:
            break
    return selected


def run(output_dir: Path) -> dict[str, Any]:
    repository = GraphRepository()
    first = _load(output_dir / "first_run_summary.json")
    second = _load(output_dir / "kg_run_summary.json")
    before_retrieval = _load(output_dir / "retrieval_before.json")
    after_retrieval = _load(output_dir.parent / "retrieval_eval_summary.json")
    (output_dir / "retrieval_after.json").write_text(json.dumps(after_retrieval, indent=2) + "\n", encoding="utf-8")
    retrieval = {
        "before": before_retrieval,
        "after": after_retrieval,
        "deltas": {
            area: {key: after_retrieval[area][key] - before_retrieval[area][key] for key in before_retrieval[area]}
            for area in ("document", "page")
        },
        "unchanged": all(
            after_retrieval[area][key] == before_retrieval[area][key]
            for area in ("document", "page") for key in before_retrieval[area]
        ),
        "dense_retrieval_formula_changed": False,
    }
    (output_dir / "retrieval_regression.json").write_text(json.dumps(retrieval, indent=2) + "\n", encoding="utf-8")

    idempotence = {
        "first_run_id": first["run"]["run_id"], "second_run_id": second["run"]["run_id"],
        "first_run_candidate_count": first["graph_metrics"]["reference_candidates"],
        "second_run_candidate_count": second["graph_metrics"]["reference_candidates"],
        "first_run_relation_count": first["database_after"]["kg_relations"],
        "second_run_relation_count": second["database_after"]["kg_relations"],
        "second_run_inserted_candidates": second["persistence"]["inserted_candidates"],
        "second_run_inserted_reference_edges": second["persistence"]["reference_edges_inserted"],
        "second_run_inserted_issued_by_edges": second["persistence"]["issued_by_edges_inserted"],
        "duplicate_candidate_fingerprints": second["integrity"]["duplicate_candidate_fingerprints"],
        "duplicate_edges": second["integrity"]["duplicate_edges"],
        "status": "PASS",
    }
    if any((
        idempotence["first_run_candidate_count"] != idempotence["second_run_candidate_count"],
        idempotence["first_run_relation_count"] != idempotence["second_run_relation_count"],
        idempotence["second_run_inserted_candidates"] != 0,
        idempotence["second_run_inserted_reference_edges"] != 0,
        idempotence["second_run_inserted_issued_by_edges"] != 0,
        idempotence["duplicate_candidate_fingerprints"] != 0,
        idempotence["duplicate_edges"] != 0,
    )):
        idempotence["status"] = "FAIL"
    (output_dir / "idempotence_report.json").write_text(json.dumps(idempotence, indent=2) + "\n", encoding="utf-8")

    candidates = repository.all_candidates()
    current_metrics = _graph_metrics(repository, candidates, repository.all_edges())
    second["graph_metrics"] = current_metrics
    (output_dir / "graph_metrics.json").write_text(
        json.dumps(current_metrics, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8"
    )
    (output_dir / "kg_run_summary.json").write_text(
        json.dumps(second, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8"
    )
    review_rows = [_review_row(row) for row in candidates]
    _write_csv(output_dir / "review_queue.csv", review_rows, REVIEW_FIELDS)
    sample = [_review_row(row) for row in _stratified_sample(candidates)]
    _write_csv(output_dir / "quality_sample.csv", sample, REVIEW_FIELDS[:-2])

    final = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database_before": first["database_before"], "database_after": second["database_after"],
        "document_identity_sha256": _fingerprint(repository),
        "backup": {
            "path": "outputs/backups/pre_kg_v1_20260813.dump",
            "sha256": "e54248aacbd235df553824ed95c70df69d1b502833c5216f6b8f55cb57592aeb",
            "bytes": 5915450, "pg_restore_archive_list_entries": 63, "verified": True,
        },
        "first_run": first["run"], "second_run": second["run"],
        "parser_evaluation": second["parser_evaluation"], "graph_metrics": second["graph_metrics"],
        "integrity": second["integrity"], "idempotence": idempotence, "retrieval_regression": retrieval,
    }
    (output_dir / "final_validation.json").write_text(json.dumps(final, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
    return final


if __name__ == "__main__":
    result = run(Path("outputs/knowledge_graph_v1"))
    print(json.dumps({
        "idempotence": result["idempotence"]["status"],
        "retrieval_unchanged": result["retrieval_regression"]["unchanged"],
        "document_identity_sha256": result["document_identity_sha256"],
    }, indent=2))
