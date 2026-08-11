from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT
from .db import Database
from .embedder import Embedder


DEFAULT_TOP_K = 5
INPUT_PATH = PROJECT_ROOT / "data" / "questions.csv"
OUTPUT_PATH = PROJECT_ROOT / "outputs" / "answers.csv"


@dataclass(frozen=True)
class QuestionInput:
    question_id: str
    question: str
    top_k: int = DEFAULT_TOP_K
    department: str | None = None
    validation_error: str = ""


def load_questions(path: Path) -> tuple[list[QuestionInput], int]:
    """Load normal user questions without evaluation labels."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "question" not in {
            name.strip() for name in reader.fieldnames if name
        }:
            raise ValueError("questions.csv must contain a 'question' column")

        questions: list[QuestionInput] = []
        skipped_blank = 0
        for row_number, row in enumerate(reader, start=2):
            question = (row.get("question") or "").strip()
            if not question:
                skipped_blank += 1
                continue

            question_id = (row.get("question_id") or "").strip()
            if not question_id:
                question_id = f"Q{len(questions) + 1:03d}"

            top_k_text = (row.get("top_k") or "").strip()
            top_k = DEFAULT_TOP_K
            validation_error = ""
            if top_k_text:
                try:
                    top_k = int(top_k_text)
                    if top_k <= 0:
                        raise ValueError
                except ValueError:
                    top_k = DEFAULT_TOP_K
                    validation_error = (
                        f"Row {row_number}: top_k must be a positive integer; "
                        f"received {top_k_text!r}"
                    )

            department = (row.get("department") or "").strip() or None
            questions.append(
                QuestionInput(
                    question_id=question_id,
                    question=question,
                    top_k=top_k,
                    department=department,
                    validation_error=validation_error,
                )
            )
    return questions, skipped_blank


def _page_label(row: dict[str, Any]) -> str:
    start = int(row["page_start"])
    end = int(row["page_end"])
    return str(start) if start == end else f"{start}-{end}"


def _date_label(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def output_fieldnames(result_slots: int) -> list[str]:
    fields = [
        "question_id",
        "question",
        "status",
        "top_k",
        "best_file",
        "best_gr_number",
        "best_gr_date",
        "best_department",
        "best_pages",
        "best_similarity",
        "best_finding",
    ]
    for rank in range(1, result_slots + 1):
        fields.extend(
            [
                f"result_{rank}_file",
                f"result_{rank}_pages",
                f"result_{rank}_similarity",
                f"result_{rank}_text",
            ]
        )
    fields.append("error")
    return fields


def build_output_row(
    item: QuestionInput,
    results: list[dict[str, Any]] | None = None,
    error: str = "",
    result_slots: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    row: dict[str, Any] = {field: "" for field in output_fieldnames(result_slots)}
    row.update(
        {
            "question_id": item.question_id,
            "question": item.question,
            "top_k": item.top_k,
        }
    )
    if error:
        row["status"] = "ERROR"
        row["error"] = error
        return row

    results = results or []
    if not results:
        row["status"] = "NO_RESULTS"
        return row

    row["status"] = "OK"
    best = results[0]
    row.update(
        {
            "best_file": best["file_name"],
            "best_gr_number": best.get("gr_number") or "",
            "best_gr_date": _date_label(best.get("gr_date")),
            "best_department": best.get("department") or "",
            "best_pages": _page_label(best),
            "best_similarity": f"{float(best['similarity']):.6f}",
            "best_finding": best.get("text") or "",
        }
    )
    for rank, result in enumerate(results[:result_slots], start=1):
        row[f"result_{rank}_file"] = result["file_name"]
        row[f"result_{rank}_pages"] = _page_label(result)
        row[f"result_{rank}_similarity"] = f"{float(result['similarity']):.6f}"
        row[f"result_{rank}_text"] = result.get("text") or ""
    return row


def process_questions(
    questions: list[QuestionInput],
    embedder: Any,
    database: Any,
    progress: Callable[[int, int, QuestionInput, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Batch-embed once, then isolate search failures to individual questions."""
    result_slots = max(DEFAULT_TOP_K, *(item.top_k for item in questions)) if questions else DEFAULT_TOP_K
    vectors: dict[int, Any] = {}
    embedding_errors: dict[int, str] = {}
    valid_indexes = [i for i, item in enumerate(questions) if not item.validation_error]

    if valid_indexes:
        texts = [questions[i].question for i in valid_indexes]
        try:
            encoded = embedder.encode(texts, show_progress_bar=False)
            if len(encoded) != len(valid_indexes):
                raise ValueError(
                    f"Embedding count mismatch: expected {len(valid_indexes)}, got {len(encoded)}"
                )
            vectors.update(zip(valid_indexes, encoded, strict=True))
        except Exception as batch_error:
            # Preserve row isolation if a particular input caused a batch failure.
            for index in valid_indexes:
                try:
                    vectors[index] = embedder.encode(
                        [questions[index].question], show_progress_bar=False
                    )[0]
                except Exception as error:
                    embedding_errors[index] = f"Embedding failed: {error or batch_error}"

    rows: list[dict[str, Any]] = []
    total = len(questions)
    for index, item in enumerate(questions):
        if item.validation_error:
            row = build_output_row(item, error=item.validation_error, result_slots=result_slots)
        elif index in embedding_errors:
            row = build_output_row(
                item, error=embedding_errors[index], result_slots=result_slots
            )
        else:
            try:
                results = database.dense_search(
                    vectors[index], top_k=item.top_k, department=item.department
                )
                row = build_output_row(item, results=results, result_slots=result_slots)
            except Exception as error:
                row = build_output_row(
                    item, error=f"Search failed: {error}", result_slots=result_slots
                )
        rows.append(row)
        if progress:
            progress(index + 1, total, item, row)
    return rows


def write_answers_atomic(path: Path, rows: list[dict[str, Any]], result_slots: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=output_fieldnames(result_slots))
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _print_progress(index: int, total: int, item: QuestionInput, row: dict[str, Any]) -> None:
    preview = item.question if len(item.question) <= 60 else item.question[:59] + "…"
    print(f"[{index}/{total}] Searching: {preview}")
    if row["status"] == "OK":
        print(f"       OK - best match: {row['best_file']}")
    elif row["status"] == "NO_RESULTS":
        print("       NO RESULTS")
    else:
        print(f"       ERROR - {row['error']}")


def main() -> None:
    questions, skipped_blank = load_questions(INPUT_PATH)
    print(f"Found {len(questions)} questions.")
    if skipped_blank:
        print(f"Skipped {skipped_blank} blank row(s).")

    rows: list[dict[str, Any]]
    if questions:
        print("Loading BGE-M3...")
        try:
            embedder = Embedder()
            database = Database()
            with database.connect() as connection:
                connection.execute("SELECT 1")
            print("Connected to PostgreSQL.\n")
            rows = process_questions(questions, embedder, database, _print_progress)
        except Exception as error:
            message = f"Startup failed: {error}"
            result_slots = max(DEFAULT_TOP_K, *(item.top_k for item in questions))
            rows = [
                build_output_row(item, error=message, result_slots=result_slots)
                for item in questions
            ]
            for index, (item, row) in enumerate(zip(questions, rows, strict=True), start=1):
                _print_progress(index, len(questions), item, row)
    else:
        rows = []

    result_slots = max(DEFAULT_TOP_K, *(item.top_k for item in questions)) if questions else DEFAULT_TOP_K
    write_answers_atomic(OUTPUT_PATH, rows, result_slots)
    successful = sum(row["status"] == "OK" for row in rows)
    failed = sum(row["status"] == "ERROR" for row in rows)
    print("\nDONE\n")
    print(f"Questions processed : {len(rows)}")
    print(f"Successful          : {successful}")
    print(f"Failed              : {failed}")
    print(f"Output              : {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
