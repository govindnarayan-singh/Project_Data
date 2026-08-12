from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from knowledge_engine.extraction.text_quality import digital_text_classification
from knowledge_engine.settings import settings


SUCCESS_STATUSES = {"SUCCESS", "SUCCESS_WITH_WARNING"}
BAD_PAGE_CLASSIFICATIONS = {
    "EMPTY_OR_IMAGE_ONLY",
    "CORRUPTED_MARATHI",
    "SUSPICIOUS_GARBLED_LATIN",
    "UNCERTAIN",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _number(row: dict[str, str], name: str, kind: type = float):
    return kind(row.get(name) or 0)


def _warning(row: dict[str, str]) -> str:
    if row["status"].startswith("FAILED"):
        return row.get("error_message", "")
    warnings = []
    for field in ("gr_number", "gr_date", "subject"):
        status = row.get(f"{field}_validation", "")
        if status and status != "VALID":
            warnings.append(
                f"{field}={status}: {row.get(f'{field}_validation_reason', '')}"
            )
    return " | ".join(warnings)


def generate(
    manifest_path: Path,
    batch_results_path: Path,
    baseline_identities_path: Path,
    output_dir: Path,
    wall_clock_seconds: float,
) -> dict[str, Any]:
    import psycopg

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_csv(manifest_path)
    results = _read_csv(batch_results_path)
    baseline_identities = _read_csv(baseline_identities_path)
    if len(manifest) != 50 or len(results) != 50:
        raise ValueError(
            f"Expected 50 manifest and result rows, got {len(manifest)} and {len(results)}"
        )

    shutil.copyfile(manifest_path, output_dir / "manifest_snapshot.csv")
    shutil.copyfile(batch_results_path, output_dir / "batch_results.csv")

    manifest_paths = {str(Path(row["file_path"])) for row in manifest}
    baseline_by_id = {
        int(row["document_id"]): (row["file_path"], row["sha256"])
        for row in baseline_identities
    }

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        documents = list(
            connection.execute(
                """SELECT id, file_path, file_name, department, sha256, page_count,
                          digital_pages, ocr_pages, extraction_method, normalized_text
                   FROM documents ORDER BY id"""
            ).fetchall()
        )
        new_documents = [row for row in documents if row["file_path"] in manifest_paths]
        new_ids = [int(row["id"]) for row in new_documents]
        pages = list(
            connection.execute(
                """SELECT document_id, page_number, extraction_method, quality_score, text
                   FROM pages WHERE document_id = ANY(%s)
                   ORDER BY document_id, page_number""",
                (new_ids,),
            ).fetchall()
        )
        chunk_rows = list(
            connection.execute(
                """SELECT d.file_name, c.document_id, c.chunk_index, c.page_start,
                          c.page_end, c.token_count
                   FROM chunks c JOIN documents d ON d.id = c.document_id
                   WHERE c.document_id = ANY(%s)
                   ORDER BY c.document_id, c.chunk_index""",
                (new_ids,),
            ).fetchall()
        )

        def scalar(sql: str) -> int:
            row = connection.execute(sql).fetchone()
            return int(next(iter(row.values())))

        integrity = {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "documents": scalar("SELECT count(*) FROM documents"),
            "pages": scalar("SELECT count(*) FROM pages"),
            "chunks": scalar("SELECT count(*) FROM chunks"),
            "embeddings": scalar("SELECT count(embedding) FROM chunks"),
            "duplicate_paths": scalar(
                "SELECT count(*) FROM (SELECT file_path FROM documents GROUP BY file_path HAVING count(*) > 1) s"
            ),
            "duplicate_sha256": scalar(
                "SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*) > 1) s"
            ),
            "invalid_page_references": scalar(
                """SELECT count(*) FROM chunks c JOIN documents d ON d.id=c.document_id
                   WHERE c.page_start < 1 OR c.page_end < c.page_start OR c.page_end > d.page_count"""
            ),
            "noncontiguous_page_sets": scalar(
                """SELECT count(*) FROM documents d
                   WHERE NOT EXISTS (SELECT 1 FROM pages p WHERE p.document_id=d.id)
                      OR (SELECT count(*) FROM pages p WHERE p.document_id=d.id) <> d.page_count
                      OR (SELECT count(DISTINCT page_number) FROM pages p WHERE p.document_id=d.id) <> d.page_count
                      OR (SELECT min(page_number) FROM pages p WHERE p.document_id=d.id) <> 1
                      OR (SELECT max(page_number) FROM pages p WHERE p.document_id=d.id) <> d.page_count"""
            ),
            "chunks_over_700": scalar("SELECT count(*) FROM chunks WHERE token_count > 700"),
            "missing_embeddings": scalar("SELECT count(*) FROM chunks WHERE embedding IS NULL"),
            "wrong_embedding_dimensions": scalar(
                "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL AND vector_dims(embedding) <> 1024"
            ),
            "documents_without_chunks": scalar(
                "SELECT count(*) FROM documents d WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id=d.id)"
            ),
            "documents_without_pages": scalar(
                "SELECT count(*) FROM documents d WHERE NOT EXISTS (SELECT 1 FROM pages p WHERE p.document_id=d.id)"
            ),
        }

    if len(new_documents) != 50:
        raise ValueError(f"Database contains {len(new_documents)} manifest documents, expected 50")

    current_by_id = {
        int(row["id"]): (row["file_path"], row["sha256"]) for row in documents
    }
    changed_originals = [
        document_id
        for document_id, identity in baseline_by_id.items()
        if current_by_id.get(document_id) != identity
    ]
    nonmanifest_additions = [
        row["file_path"]
        for row in documents
        if int(row["id"]) not in baseline_by_id and row["file_path"] not in manifest_paths
    ]
    integrity.update(
        {
            "new_documents": len(new_documents),
            "new_pages": len(pages),
            "new_chunks": len(chunk_rows),
            "new_embeddings": sum(_number(row, "embedding_count", int) for row in results),
            "original_document_ids_unchanged": not changed_originals,
            "changed_original_document_ids": changed_originals,
            "existing_documents_reprocessed": sum(
                row.get("operation") == "REPROCESSED" for row in results
            ),
            "nonmanifest_documents_added": nonmanifest_additions,
            "manifest_document_count_matches": len(new_documents) == 50,
        }
    )

    page_classes: dict[int, Counter[str]] = defaultdict(Counter)
    for page in pages:
        page_classes[int(page["document_id"])][
            digital_text_classification(page["text"] or "")
        ] += 1

    document_by_path = {row["file_path"]: row for row in new_documents}
    result_table: list[dict[str, Any]] = []
    extraction_rows: list[dict[str, Any]] = []
    metadata_issues: list[dict[str, Any]] = []
    for row in results:
        database_path = str(Path(row["file_path"]))
        document = document_by_path.get(database_path)
        document_id = int(document["id"]) if document else 0
        total = _number(row, "pages_total", int)
        digital = _number(row, "pages_digital", int)
        ocr = _number(row, "pages_ocr", int)
        classifications = page_classes[document_id]
        bad_count = sum(
            count
            for classification, count in classifications.items()
            if classification in BAD_PAGE_CLASSIFICATIONS
        )
        extraction_rows.append(
            {
                "file_name": row["file_name"],
                "department": row["department"],
                "pages_total": total,
                "pages_digital": digital,
                "pages_ocr": ocr,
                "digital_percentage": round(100 * digital / total, 3) if total else 0,
                "ocr_percentage": round(100 * ocr / total, 3) if total else 0,
                "mixed_digital_ocr": digital > 0 and ocr > 0,
                "extraction_failures": int(row["status"] == "FAILED_EXTRACTION"),
                "final_bad_or_uncertain_pages": bad_count,
                "final_page_classifications": json.dumps(dict(classifications), ensure_ascii=False, sort_keys=True),
                "extraction_elapsed_seconds": _number(row, "extraction_elapsed_seconds"),
            }
        )
        for field in ("gr_number", "gr_date", "subject"):
            status = row.get(f"{field}_validation", "")
            if status in {"SUSPICIOUS", "MISSING"}:
                metadata_issues.append(
                    {
                        "filename": row["file_name"],
                        "department": row["department"],
                        "field": field,
                        "extracted_raw_value": row.get(field, ""),
                        "validation_status": status,
                        "validation_reason": row.get(
                            f"{field}_validation_reason", ""
                        ),
                    }
                )
        result_table.append(
            {
                "file_name": row["file_name"],
                "department": row["department"],
                "pages_total": total,
                "pages_digital": digital,
                "pages_ocr": ocr,
                "gr_number": row["gr_number"],
                "gr_number_status": row["gr_number_validation"],
                "gr_date": row["gr_date"],
                "gr_date_status": row["gr_date_validation"],
                "subject": row["subject"],
                "subject_status": row["subject_validation"],
                "chunks_created": _number(row, "chunks_created", int),
                "min_chunk_tokens": _number(row, "min_chunk_tokens", int),
                "max_chunk_tokens": _number(row, "max_chunk_tokens", int),
                "avg_chunk_tokens": _number(row, "avg_chunk_tokens"),
                "embedding_count": _number(row, "embedding_count", int),
                "embedding_dimension": _number(row, "embedding_dimension", int),
                "elapsed_seconds": _number(row, "elapsed_seconds"),
                "operation": row["operation"],
                "status": row["status"],
                "warning/error": _warning(row),
            }
        )

    result_fields = list(result_table[0])
    _write_csv(output_dir / "results.csv", result_fields, result_table)
    _write_csv(
        output_dir / "metadata_issues.csv",
        [
            "filename",
            "department",
            "field",
            "extracted_raw_value",
            "validation_status",
            "validation_reason",
        ],
        metadata_issues,
    )
    extraction_fields = list(extraction_rows[0])
    _write_csv(output_dir / "extraction_summary.csv", extraction_fields, extraction_rows)

    chunk_counts_by_file = Counter(row["file_name"] for row in chunk_rows)
    chunk_tokens = [int(row["token_count"]) for row in chunk_rows]
    low_text_chunks = [
        {
            "file_name": row["file_name"],
            "chunk_index": int(row["chunk_index"]),
            "page_start": int(row["page_start"]),
            "page_end": int(row["page_end"]),
            "token_count": int(row["token_count"]),
        }
        for row in chunk_rows
        if int(row["token_count"]) < 50
    ]
    high_chunk_documents = sorted(
        (
            {
                "file_name": row["file_name"],
                "department": row["department"],
                "pages": int(row["page_count"]),
                "chunks": chunk_counts_by_file[row["file_name"]],
                "chunks_per_page": round(
                    chunk_counts_by_file[row["file_name"]] / int(row["page_count"]), 3
                ),
                "annexure_table_markers": [
                    marker
                    for marker in ("annexure", "appendix", "परिशिष्ट", "तक्ता")
                    if marker.casefold() in (row["normalized_text"] or "").casefold()
                ],
            }
            for row in new_documents
        ),
        key=lambda item: (-item["chunks"], item["file_name"]),
    )

    statuses = Counter(row["status"] for row in results)
    metadata_counts = {
        field: dict(Counter(row[f"{field}_validation"] for row in results))
        for field in ("gr_number", "gr_date", "subject")
    }
    extraction_total = sum(row["pages_total"] for row in extraction_rows)
    digital_total = sum(row["pages_digital"] for row in extraction_rows)
    ocr_total = sum(row["pages_ocr"] for row in extraction_rows)
    document_elapsed = [_number(row, "elapsed_seconds") for row in results]
    extraction_elapsed = sum(
        _number(row, "extraction_elapsed_seconds") for row in results
    )
    embedding_elapsed = sum(
        _number(row, "embedding_elapsed_seconds") for row in results
    )
    successful = [row for row in results if row["status"] in SUCCESS_STATUSES]
    failures = [row for row in results if row["status"].startswith("FAILED")]
    failure_rows = [
        {
            "filename": row["file_name"],
            "department": row["department"],
            "stage": row["error_stage"],
            "exception/error": row["error_message"],
            "database_rollback_occurred": row["error_stage"] == "database",
            "partial_rows_remaining": False,
            "failure_group": row["error_stage"],
            "recommended_next_action": "Inspect and resume the same frozen manifest",
        }
        for row in failures
    ]
    _write_csv(
        output_dir / "failure_analysis.csv",
        [
            "filename",
            "department",
            "stage",
            "exception/error",
            "database_rollback_occurred",
            "partial_rows_remaining",
            "failure_group",
            "recommended_next_action",
        ],
        failure_rows,
    )

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "manifest_sha256": __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_documents": 50,
        "status_counts": dict(statuses),
        "failure_count": len(failures),
        "metadata_validation": metadata_counts,
        "metadata_issue_count": len(metadata_issues),
        "extraction": {
            "total_pages": extraction_total,
            "digital_pages": digital_total,
            "ocr_pages": ocr_total,
            "digital_percentage": round(100 * digital_total / extraction_total, 3),
            "ocr_percentage": round(100 * ocr_total / extraction_total, 3),
            "mixed_extraction_documents": sum(row["mixed_digital_ocr"] for row in extraction_rows),
            "image_heavy_documents_80_percent_ocr": [
                row["file_name"] for row in extraction_rows if row["ocr_percentage"] >= 80
            ],
            "extraction_failures": sum(row["extraction_failures"] for row in extraction_rows),
            "final_bad_or_uncertain_pages": sum(row["final_bad_or_uncertain_pages"] for row in extraction_rows),
            "top_10_ocr_percentage": sorted(
                extraction_rows,
                key=lambda row: (-row["ocr_percentage"], -row["pages_total"], row["file_name"]),
            )[:10],
        },
        "chunking": {
            "total_new_chunks": len(chunk_rows),
            "average_chunks_per_successful_document": round(len(chunk_rows) / len(successful), 3),
            "global_min_tokens": min(chunk_tokens),
            "global_max_tokens": max(chunk_tokens),
            "global_average_tokens": round(statistics.fmean(chunk_tokens), 3),
            "chunks_over_700": sum(token > 700 for token in chunk_tokens),
            "top_10_documents_by_chunks": high_chunk_documents[:10],
            "low_text_chunks_below_50_tokens": low_text_chunks,
            "high_chunk_documents_with_annexure_table_markers": [
                row for row in high_chunk_documents[:10] if row["annexure_table_markers"]
            ],
        },
        "embeddings": {
            "new_embeddings": sum(_number(row, "embedding_count", int) for row in successful),
            "dimension": 1024,
            "wrong_dimensions": integrity["wrong_embedding_dimensions"],
            "missing": integrity["missing_embeddings"],
        },
        "performance": {
            "wall_clock_seconds": wall_clock_seconds,
            "batch_document_elapsed_seconds": round(sum(document_elapsed), 3),
            "extraction_elapsed_seconds": round(extraction_elapsed, 3),
            "embedding_elapsed_seconds": round(embedding_elapsed, 3),
            "average_seconds_per_document": round(statistics.fmean(document_elapsed), 3),
            "median_seconds_per_document": round(statistics.median(document_elapsed), 3),
            "pages_per_minute_wall_clock": round(extraction_total / wall_clock_seconds * 60, 3),
            "ocr_pages_per_minute_extraction_time": round(ocr_total / extraction_elapsed * 60, 3),
            "embedding_chunks_per_second": round(len(chunk_rows) / embedding_elapsed, 3),
            "slowest_10_documents": sorted(
                (
                    {"file_name": row["file_name"], "elapsed_seconds": _number(row, "elapsed_seconds")}
                    for row in results
                ),
                key=lambda row: (-row["elapsed_seconds"], row["file_name"]),
            )[:10],
        },
        "integrity": integrity,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "integrity_report.json").write_text(
        json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Generate corpus-expansion evidence artifacts")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("batch_results", type=Path)
    parser.add_argument("baseline_identities", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--wall-clock-seconds", type=float, required=True)
    args = parser.parse_args()
    summary = generate(
        args.manifest.resolve(),
        args.batch_results.resolve(),
        args.baseline_identities.resolve(),
        args.output_dir.resolve(),
        args.wall_clock_seconds,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
