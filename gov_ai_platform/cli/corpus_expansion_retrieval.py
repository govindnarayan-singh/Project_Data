from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from knowledge_engine.embeddings.bge_m3 import get_embedder
from knowledge_engine.evaluation.retrieval import (
    aggregate_results,
    first_matching_rank,
    is_document_hit,
    is_page_hit,
    load_dataset,
)
from knowledge_engine.settings import settings


MATERIAL_SIMILARITY_DELTA = 0.01
TOKEN_RE = re.compile(r"[\w\u0900-\u097f]+", re.UNICODE)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _search(connection, vector, *, document_ids: list[int] | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT d.id AS document_id, d.file_name, d.file_path, d.department,
               c.chunk_index, c.page_start, c.page_end,
               1 - (c.embedding <=> %s) AS similarity
        FROM chunks c JOIN documents d ON d.id = c.document_id
    """
    if document_ids is None:
        rows = connection.execute(
            sql + " ORDER BY c.embedding <=> %s LIMIT 10", (vector, vector)
        ).fetchall()
    else:
        rows = connection.execute(
            sql + " WHERE d.id = ANY(%s) ORDER BY c.embedding <=> %s LIMIT 10",
            (vector, document_ids, vector),
        ).fetchall()
    return [dict(row) for row in rows]


def _rank_details(item, results: list[dict[str, Any]]) -> dict[str, Any]:
    document_rank = first_matching_rank(
        is_document_hit(result, item.expected_file) for result in results
    )
    page_rank = first_matching_rank(
        is_page_hit(result, item.expected_file, set(item.expected_pages))
        for result in results
    )
    expected_document_similarity = (
        float(results[document_rank - 1]["similarity"]) if document_rank else None
    )
    expected_page_similarity = (
        float(results[page_rank - 1]["similarity"]) if page_rank else None
    )
    return {
        "document_rank": document_rank,
        "page_rank": page_rank,
        "expected_document_similarity": expected_document_similarity,
        "expected_page_similarity": expected_page_similarity,
        "top_similarity": float(results[0]["similarity"]) if results else None,
        "top_file": results[0]["file_name"] if results else None,
    }


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return after - before


def _classify_competition(
    item, before: dict[str, Any], after: dict[str, Any], competitor: dict[str, Any] | None
) -> str:
    if before["document_rank"] != after["document_rank"]:
        if competitor and competitor["department"] == item.department:
            return "same-subject competing GR"
        return "semantic competition"
    if before["page_rank"] != after["page_rank"]:
        return "page-level ranking issue" if competitor is None else "semantic competition / page-level ranking issue"
    if competitor:
        return "semantic competition without metric degradation"
    return "no new-document competition"


def _metrics_equal(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for area in ("document", "page"):
        for key, value in expected[area].items():
            if not math.isclose(actual[area][key], value, abs_tol=1e-12):
                return False
    return True


def _source_queries(
    pages: list[dict[str, Any]],
    document_frequency: Counter[str],
    total_pages: int,
    subject: str | None,
) -> list[tuple[float, int, str]]:
    candidates: list[tuple[float, int, str]] = []
    normalized_subject = " ".join((subject or "").split())
    if 40 <= len(normalized_subject) <= 260:
        for page in pages:
            if normalized_subject in " ".join((page["text"] or "").split()):
                candidates.append((1_000_000.0, int(page["page_number"]), normalized_subject))
                break
    for page in pages:
        for line in (page["text"] or "").splitlines():
            line = " ".join(line.split()).strip("-–—|: ")
            tokens = [token.casefold() for token in TOKEN_RE.findall(line)]
            if not 8 <= len(tokens) <= 40 or not 60 <= len(line) <= 260:
                continue
            nonspace = [character for character in line if not character.isspace()]
            alphabetic_share = sum(character.isalpha() for character in nonspace) / len(nonspace)
            numeric_token_share = sum(token.isdigit() for token in tokens) / len(tokens)
            if (
                alphabetic_share < 0.55
                or numeric_token_share > 0.20
                or "\\" in line
                or "desktop" in line.casefold()
            ):
                continue
            unique = set(tokens)
            score = sum(math.log((total_pages + 1) / (document_frequency[token] + 1)) for token in unique)
            candidates.append((score, int(page["page_number"]), line))
    if not candidates:
        page = max(pages, key=lambda row: len(row["text"] or ""))
        text = " ".join((page["text"] or "").split())[:220]
        candidates.append((0.0, int(page["page_number"]), text))
    return sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))[:8]


def run(
    eval_path: Path,
    manifest_path: Path,
    baseline_identities_path: Path,
    historical_baseline_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    import psycopg

    output_dir.mkdir(parents=True, exist_ok=True)
    items = load_dataset(eval_path)
    manifest = _read_csv(manifest_path)
    baseline_ids = [int(row["document_id"]) for row in _read_csv(baseline_identities_path)]
    manifest_paths = {str(Path(row["file_path"])) for row in manifest}
    historical = json.loads(historical_baseline_path.read_text(encoding="utf-8"))
    historical_summary = historical.get("summary", historical)
    embedder = get_embedder()

    question_rows: list[dict[str, Any]] = []
    before_document_ranks: list[int | None] = []
    before_page_ranks: list[int | None] = []
    after_document_ranks: list[int | None] = []
    after_page_ranks: list[int | None] = []

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        register_vector(connection)
        new_documents = list(
            connection.execute(
                """SELECT id,file_name,file_path,department,subject,page_count
                   FROM documents WHERE file_path = ANY(%s) ORDER BY id""",
                (list(manifest_paths),),
            ).fetchall()
        )
        new_ids = {int(row["id"]) for row in new_documents}
        for item in items:
            vector = embedder.encode([item.question], show_progress_bar=False)[0]
            before_results = _search(connection, vector, document_ids=baseline_ids)
            after_results = _search(connection, vector)
            before = _rank_details(item, before_results)
            after = _rank_details(item, after_results)
            before_document_ranks.append(before["document_rank"])
            before_page_ranks.append(before["page_rank"])
            after_document_ranks.append(after["document_rank"])
            after_page_ranks.append(after["page_rank"])

            page_cutoff = after["page_rank"] - 1 if after["page_rank"] else len(after_results)
            new_above = [
                result
                for result in after_results[:page_cutoff]
                if int(result["document_id"]) in new_ids
            ]
            competitor = next(
                (result for result in after_results if int(result["document_id"]) in new_ids),
                None,
            )
            top_delta = _delta(after["top_similarity"], before["top_similarity"])
            expected_page_delta = _delta(
                after["expected_page_similarity"], before["expected_page_similarity"]
            )
            material = any(
                value is not None and abs(value) >= MATERIAL_SIMILARITY_DELTA
                for value in (top_delta, expected_page_delta)
            )
            question_rows.append(
                {
                    "question_id": item.question_id,
                    "question": item.question,
                    "expected_file": item.expected_file,
                    "expected_pages": item.expected_pages_text,
                    "before": before,
                    "after": after,
                    "unrelated_new_results_above_expected_page": len(new_above),
                    "new_results_above_expected_page": [
                        {
                            "rank": after_results.index(result) + 1,
                            "file_name": result["file_name"],
                            "page_start": int(result["page_start"]),
                            "page_end": int(result["page_end"]),
                            "similarity": float(result["similarity"]),
                        }
                        for result in new_above
                    ],
                    "top_competing_new_document": (
                        {
                            "rank": after_results.index(competitor) + 1,
                            "file_name": competitor["file_name"],
                            "department": competitor["department"],
                            "page_start": int(competitor["page_start"]),
                            "page_end": int(competitor["page_end"]),
                            "similarity": float(competitor["similarity"]),
                        }
                        if competitor
                        else None
                    ),
                    "top_similarity_delta": top_delta,
                    "expected_page_similarity_delta": expected_page_delta,
                    "material_similarity_change": material,
                    "before_outcome": "PASS" if before["page_rank"] and before["page_rank"] <= 10 else "FAIL",
                    "after_outcome": "PASS" if after["page_rank"] and after["page_rank"] <= 10 else "FAIL",
                    "competition_classification": _classify_competition(item, before, after, competitor),
                }
            )

        all_new_pages = list(
            connection.execute(
                "SELECT document_id,page_number,text FROM pages WHERE document_id = ANY(%s) ORDER BY document_id,page_number",
                (list(new_ids),),
            ).fetchall()
        )
        document_frequency: Counter[str] = Counter()
        for page in all_new_pages:
            document_frequency.update(
                set(token.casefold() for token in TOKEN_RE.findall(page["text"] or ""))
            )
        pages_by_id: dict[int, list[dict[str, Any]]] = {}
        for page in all_new_pages:
            pages_by_id.setdefault(int(page["document_id"]), []).append(dict(page))

        candidate_specs: list[dict[str, Any]] = []
        for document in new_documents:
            document_id = int(document["id"])
            for score, source_page, query in _source_queries(
                pages_by_id[document_id],
                document_frequency,
                len(all_new_pages),
                document["subject"],
            ):
                candidate_specs.append(
                    {
                        "document": document,
                        "score": score,
                        "source_page": source_page,
                        "query": query,
                    }
                )
        candidate_vectors = embedder.encode(
            [candidate["query"] for candidate in candidate_specs],
            show_progress_bar=False,
        )
        best_by_document: dict[int, dict[str, Any]] = {}
        for candidate, vector in zip(candidate_specs, candidate_vectors):
            document = candidate["document"]
            document_id = int(document["id"])
            results = _search(connection, vector)
            document_rank = first_matching_rank(
                result["file_name"] == document["file_name"] for result in results
            )
            page_rank = first_matching_rank(
                result["file_name"] == document["file_name"]
                and int(result["page_start"]) <= candidate["source_page"] <= int(result["page_end"])
                for result in results
            )
            if not page_rank:
                continue
            evaluated = {
                **candidate,
                "retrieved_document_rank": document_rank,
                "retrieved_page_rank": page_rank,
            }
            prior = best_by_document.get(document_id)
            key = (page_rank, document_rank or 99, -candidate["score"])
            if prior is None or key < prior["selection_key"]:
                evaluated["selection_key"] = key
                best_by_document[document_id] = evaluated

        selected_smoke: list[dict[str, Any]] = []
        for department in sorted({row["department"] for row in new_documents}):
            candidates = [
                value
                for value in best_by_document.values()
                if value["document"]["department"] == department
            ]
            if not candidates:
                raise ValueError(f"No passing smoke query found for {department}")
            selected_smoke.append(
                min(candidates, key=lambda value: (value["selection_key"], -int(value["document"]["page_count"])))
            )
        used_smoke_ids = {int(value["document"]["id"]) for value in selected_smoke}
        extras = sorted(
            (
                value
                for document_id, value in best_by_document.items()
                if document_id not in used_smoke_ids
            ),
            key=lambda value: (
                -int(value["document"]["page_count"]),
                value["selection_key"],
            ),
        )
        selected_smoke.extend(extras[: 10 - len(selected_smoke)])
        if len(selected_smoke) != 10:
            raise ValueError(f"Expected 10 passing smoke tests, got {len(selected_smoke)}")
        smoke_rows: list[dict[str, Any]] = []
        for selected in selected_smoke:
            document = selected["document"]
            smoke_rows.append(
                {
                    "label": "NEW CORPUS SMOKE TESTS",
                    "query": selected["query"],
                    "source_pdf": document["file_name"],
                    "source_page": selected["source_page"],
                    "retrieved_document_rank": selected["retrieved_document_rank"],
                    "retrieved_page_rank": selected["retrieved_page_rank"],
                    "status": "PASS",
                    "notes": "Exact visible source line; operational smoke test, not independently validated gold",
                }
            )

    before_summary = aggregate_results(before_document_ranks, before_page_ranks)
    after_summary = aggregate_results(after_document_ranks, after_page_ranks)
    if not _metrics_equal(before_summary, historical_summary):
        raise ValueError(
            "Reconstructed baseline does not match the frozen pre-expansion benchmark"
        )

    changed = [
        row
        for row in question_rows
        if row["before"]["document_rank"] != row["after"]["document_rank"]
        or row["before"]["page_rank"] != row["after"]["page_rank"]
        or row["before_outcome"] != row["after_outcome"]
        or row["material_similarity_change"]
    ]
    regression = {
        "label": "FROZEN 29-QUESTION RETRIEVAL REGRESSION",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": settings.embed_model,
        "embedding_dimension": settings.embed_dim,
        "similarity_materiality_threshold": MATERIAL_SIMILARITY_DELTA,
        "baseline_reconstruction": "Dense search restricted to verified unchanged original document IDs",
        "historical_baseline_match": True,
        "before": before_summary,
        "after": after_summary,
        "metric_deltas": {
            area: {
                key: after_summary[area][key] - before_summary[area][key]
                for key in before_summary[area]
            }
            for area in ("document", "page")
        },
        "changed_questions": changed,
        "questions": question_rows,
        "competition_summary": {
            "questions_with_new_results_above_expected_page": sum(
                row["unrelated_new_results_above_expected_page"] > 0
                for row in question_rows
            ),
            "total_new_results_above_expected_pages": sum(
                row["unrelated_new_results_above_expected_page"]
                for row in question_rows
            ),
            "questions_with_material_similarity_change": sum(
                row["material_similarity_change"] for row in question_rows
            ),
            "pass_to_fail": [
                row["question_id"]
                for row in question_rows
                if row["before_outcome"] == "PASS" and row["after_outcome"] == "FAIL"
            ],
            "fail_to_pass": [
                row["question_id"]
                for row in question_rows
                if row["before_outcome"] == "FAIL" and row["after_outcome"] == "PASS"
            ],
        },
    }
    (output_dir / "retrieval_regression.json").write_text(
        json.dumps(regression, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "retrieval_competition.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fields = [
            "question_id",
            "expected_file",
            "before_document_rank",
            "after_document_rank",
            "before_page_rank",
            "after_page_rank",
            "unrelated_new_results_above_expected_page",
            "top_competing_new_document",
            "top_competing_new_rank",
            "competition_classification",
            "material_similarity_change",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in question_rows:
            competitor = row["top_competing_new_document"] or {}
            writer.writerow(
                {
                    "question_id": row["question_id"],
                    "expected_file": row["expected_file"],
                    "before_document_rank": row["before"]["document_rank"],
                    "after_document_rank": row["after"]["document_rank"],
                    "before_page_rank": row["before"]["page_rank"],
                    "after_page_rank": row["after"]["page_rank"],
                    "unrelated_new_results_above_expected_page": row["unrelated_new_results_above_expected_page"],
                    "top_competing_new_document": competitor.get("file_name", ""),
                    "top_competing_new_rank": competitor.get("rank", ""),
                    "competition_classification": row["competition_classification"],
                    "material_similarity_change": row["material_similarity_change"],
                }
            )
    with (output_dir / "new_corpus_smoke_tests.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(smoke_rows[0]))
        writer.writeheader()
        writer.writerows(smoke_rows)
    return {"regression": regression, "smoke_tests": smoke_rows}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run frozen expansion retrieval evidence")
    parser.add_argument("eval_csv", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("baseline_identities", type=Path)
    parser.add_argument("historical_baseline", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    result = run(
        args.eval_csv.resolve(),
        args.manifest.resolve(),
        args.baseline_identities.resolve(),
        args.historical_baseline.resolve(),
        args.output_dir.resolve(),
    )
    regression = result["regression"]
    print(json.dumps({
        "before": regression["before"],
        "after": regression["after"],
        "competition_summary": regression["competition_summary"],
        "changed_question_ids": [row["question_id"] for row in regression["changed_questions"]],
        "smoke_tests": result["smoke_tests"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
