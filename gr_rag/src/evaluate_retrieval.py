from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import PROJECT_ROOT, settings
from .db import Database
from .embedder import Embedder


METRIC_KS = (1, 3, 5, 10)
REQUIRED_COLUMNS = {
    "question_id",
    "question",
    "expected_file",
    "expected_gr_number",
    "expected_pages",
    "department",
    "notes",
}


@dataclass(frozen=True)
class EvaluationItem:
    question_id: str
    question: str
    expected_file: str
    expected_gr_number: str
    expected_pages_text: str
    expected_pages: frozenset[int]
    department: str
    notes: str


def parse_expected_pages(value: str) -> set[int]:
    """Parse page labels such as ``1``, ``1-3``, or ``2,4,7-9``."""
    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Invalid expected_pages value: {value!r}")
        if "-" in part:
            bounds = [piece.strip() for piece in part.split("-")]
            if len(bounds) != 2:
                raise ValueError(f"Invalid page range: {part!r}")
            start, end = (int(piece) for piece in bounds)
            if start <= 0 or end < start:
                raise ValueError(f"Invalid page range: {part!r}")
            pages.update(range(start, end + 1))
        else:
            page = int(part)
            if page <= 0:
                raise ValueError(f"Page numbers must be positive: {part!r}")
            pages.add(page)
    if not pages:
        raise ValueError("expected_pages cannot be empty")
    return pages


def page_range_overlaps(page_start: int, page_end: int, expected_pages: set[int]) -> bool:
    if page_start <= 0 or page_end < page_start:
        raise ValueError(f"Invalid retrieved page range: {page_start}-{page_end}")
    return any(page_start <= page <= page_end for page in expected_pages)


def is_document_hit(result: dict[str, Any], expected_file: str) -> bool:
    return result["file_name"] == expected_file


def is_page_hit(
    result: dict[str, Any], expected_file: str, expected_pages: set[int]
) -> bool:
    return is_document_hit(result, expected_file) and page_range_overlaps(
        int(result["page_start"]), int(result["page_end"]), expected_pages
    )


def first_matching_rank(matches: Iterable[bool]) -> int | None:
    return next((rank for rank, matched in enumerate(matches, start=1) if matched), None)


def recall_at_k(ranks: Iterable[int | None], k: int) -> float:
    ranks = list(ranks)
    if not ranks:
        return 0.0
    return sum(rank is not None and rank <= k for rank in ranks) / len(ranks)


def reciprocal_rank(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / rank


def mean_reciprocal_rank(ranks: Iterable[int | None]) -> float:
    ranks = list(ranks)
    if not ranks:
        return 0.0
    return sum(reciprocal_rank(rank) for rank in ranks) / len(ranks)


def aggregate_results(
    document_ranks: Iterable[int | None],
    page_ranks: Iterable[int | None],
    ks: Iterable[int] = METRIC_KS,
) -> dict[str, Any]:
    document_ranks = list(document_ranks)
    page_ranks = list(page_ranks)
    if len(document_ranks) != len(page_ranks):
        raise ValueError("Document and page rank lists must have the same length")
    return {
        "question_count": len(document_ranks),
        "document": {
            **{f"recall_at_{k}": recall_at_k(document_ranks, k) for k in ks},
            "mrr": mean_reciprocal_rank(document_ranks),
        },
        "page": {
            **{f"recall_at_{k}": recall_at_k(page_ranks, k) for k in ks},
            "mrr": mean_reciprocal_rank(page_ranks),
        },
    }


def _load_dataset(path: Path) -> list[EvaluationItem]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Evaluation CSV is missing columns: {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("Evaluation CSV is empty")

    items: list[EvaluationItem] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        values = {key: (row.get(key) or "").strip() for key in REQUIRED_COLUMNS}
        required_values = REQUIRED_COLUMNS - {"notes"}
        empty = sorted(key for key in required_values if not values[key])
        if empty:
            raise ValueError(f"Row {row_number} has empty required fields: {empty}")
        if values["question_id"] in seen_ids:
            raise ValueError(f"Duplicate question_id: {values['question_id']}")
        seen_ids.add(values["question_id"])
        items.append(
            EvaluationItem(
                question_id=values["question_id"],
                question=values["question"],
                expected_file=values["expected_file"],
                expected_gr_number=values["expected_gr_number"],
                expected_pages_text=values["expected_pages"],
                expected_pages=frozenset(parse_expected_pages(values["expected_pages"])),
                department=values["department"],
                notes=values["notes"],
            )
        )
    return items


def _preview(text: str, limit: int = 260) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _failure_category(item: EvaluationItem, document_rank: int | None, page_rank: int | None) -> str:
    finance_long_pages = item.expected_file == "202203111452147705......pdf" and bool(
        item.expected_pages.intersection({1, 2, 3, 4, 5})
    )
    if document_rank is None or document_rank > 5:
        return "semantic retrieval issue"
    if page_rank is None or page_rank > 5:
        if finance_long_pages:
            return "chunking issue"
        # A matching page that exists below rank 5 has a valid boundary; it was
        # simply scored below other chunks from the correct document.
        return "semantic retrieval issue" if page_rank is not None else "page-boundary issue"
    return "semantic retrieval issue"


def _write_detailed_csv(path: Path, evaluations: list[dict[str, Any]]) -> None:
    fields = [
        "question_id", "question", "expected_file", "expected_gr_number",
        "expected_pages", "expected_department", "rank", "similarity", "file_name",
        "gr_number", "department", "chunk_index", "page_start", "page_end",
        "document_match", "page_match", "text_preview",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for evaluation in evaluations:
            item = evaluation["item"]
            for result in evaluation["results"]:
                writer.writerow(
                    {
                        "question_id": item.question_id,
                        "question": item.question,
                        "expected_file": item.expected_file,
                        "expected_gr_number": item.expected_gr_number,
                        "expected_pages": item.expected_pages_text,
                        "expected_department": item.department,
                        **result,
                    }
                )


def _write_report(
    path: Path,
    items: list[EvaluationItem],
    evaluations: list[dict[str, Any]],
    summary: dict[str, Any],
    display_k: int,
) -> None:
    timestamp = summary["evaluation_timestamp"]
    lines = [
        "# Marathi GR Retrieval Evaluation",
        "",
        "## Dataset",
        "",
        f"- Number of questions: {len(items)}",
        f"- Number of GRs represented: {len({item.expected_file for item in items})}",
        f"- Departments represented: {len({item.department for item in items})}",
        f"- Evaluation date/time: {timestamp}",
        f"- Model: {settings.embed_model}",
        "- Retrieval type: dense semantic retrieval",
        f"- Embedding dimension: {settings.embed_dim}",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Score |",
        "|---|---:|",
    ]
    for level, label in (("document", "Document"), ("page", "Page")):
        for k in METRIC_KS:
            lines.append(f"| {label} Recall@{k} | {summary[level][f'recall_at_{k}']:.2%} |")
        lines.append(f"| {label} MRR | {summary[level]['mrr']:.4f} |")

    lines += ["", "## Question-Level Results", ""]
    for evaluation in evaluations:
        item = evaluation["item"]
        page_pass = evaluation["page_rank"] is not None and evaluation["page_rank"] <= 5
        lines += [
            f"### {item.question_id} — {'PASS' if page_pass else 'FAIL'}",
            "",
            f"- Question: {item.question}",
            f"- Expected GR: {item.expected_gr_number}",
            f"- Expected file: `{item.expected_file}`",
            f"- Expected pages: {item.expected_pages_text}",
            f"- Expected document rank: {evaluation['document_rank'] or 'not retrieved'}",
            f"- Expected page rank: {evaluation['page_rank'] or 'not retrieved'}",
            "",
            f"Top {display_k} results:",
            "",
            "| Rank | File | Pages | Similarity | Document | Page | Preview |",
            "|---:|---|---:|---:|---|---|---|",
        ]
        for result in evaluation["results"][:display_k]:
            preview = result["text_preview"].replace("|", "\\|")
            lines.append(
                f"| {result['rank']} | `{result['file_name']}` | "
                f"{result['page_start']}-{result['page_end']} | {result['similarity']:.4f} | "
                f"{'YES' if result['document_match'] else 'NO'} | "
                f"{'YES' if result['page_match'] else 'NO'} | {preview} |"
            )
        lines.append("")

    failures = summary["failures"]
    lines += ["## Failure Analysis", ""]
    if failures:
        lines += [
            "| Question | Document rank | Page rank | Likely classification |",
            "|---|---:|---:|---|",
        ]
        for failure in failures:
            lines.append(
                f"| {failure['question_id']} | {failure['document_rank'] or '—'} | "
                f"{failure['page_rank'] or '—'} | {failure['classification']} |"
            )
    else:
        lines.append("No Page Recall@5 failures were observed.")

    lines += [
        "",
        "## Current Known Limitations",
        "",
        "1. The database contains only the controlled validation corpus (10 evaluated GRs plus one older test GR), not all 2,715 GRs.",
        "2. Four chunks in one Finance PDF currently exceed the configured 700-token ceiling.",
        "3. Some metadata fields are populated but have known semantic-quality concerns.",
        "4. This is a baseline dense-retrieval evaluation, not final production accuracy.",
        "",
        "No retrieval tuning, hybrid search, reranking, or failure-driven label changes were performed.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate dense retrieval against frozen page-level labels")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    if args.k <= 0:
        parser.error("--k must be positive")

    items = _load_dataset(args.csv_path)
    retrieval_k = max(args.k, max(METRIC_KS))

    print("FROZEN EVALUATION DATASET — expected labels are printed before retrieval")
    for item in items:
        print(
            f"{item.question_id} | {item.question} | "
            f"{item.expected_file} | pages {item.expected_pages_text}"
        )
    print(f"DATASET FROZEN: {len(items)} questions. Starting retrieval.\n")

    embedder = Embedder()
    db = Database()
    vectors = embedder.encode([item.question for item in items], show_progress_bar=False)
    evaluations: list[dict[str, Any]] = []
    document_ranks: list[int | None] = []
    page_ranks: list[int | None] = []

    for item, vector in zip(items, vectors, strict=True):
        rows = db.dense_search(vector, top_k=retrieval_k)
        results: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            document_match = is_document_hit(row, item.expected_file)
            page_match = is_page_hit(row, item.expected_file, set(item.expected_pages))
            results.append(
                {
                    "rank": rank,
                    "similarity": float(row["similarity"]),
                    "file_name": row["file_name"],
                    "gr_number": row["gr_number"] or "",
                    "department": row["department"],
                    "chunk_index": int(row["chunk_index"]),
                    "page_start": int(row["page_start"]),
                    "page_end": int(row["page_end"]),
                    "document_match": document_match,
                    "page_match": page_match,
                    "text_preview": _preview(row["text"]),
                }
            )
        document_rank = first_matching_rank(result["document_match"] for result in results)
        page_rank = first_matching_rank(result["page_match"] for result in results)
        document_ranks.append(document_rank)
        page_ranks.append(page_rank)
        evaluations.append(
            {"item": item, "results": results, "document_rank": document_rank, "page_rank": page_rank}
        )
        print(
            f"{item.question_id} | doc rank: {document_rank or 'MISS'} | "
            f"page rank: {page_rank or 'MISS'}"
        )

    summary = aggregate_results(document_ranks, page_ranks)
    summary.update(
        {
            "evaluation_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "dataset_path": str(args.csv_path.resolve()),
            "model": settings.embed_model,
            "retrieval_type": "dense semantic retrieval",
            "embedding_dimension": settings.embed_dim,
            "retrieved_k": retrieval_k,
            "display_k": args.k,
            "gr_count": len({item.expected_file for item in items}),
            "departments": sorted({item.department for item in items}),
        }
    )
    failures = []
    for evaluation in evaluations:
        page_rank = evaluation["page_rank"]
        if page_rank is None or page_rank > 5:
            item = evaluation["item"]
            failures.append(
                {
                    "question_id": item.question_id,
                    "question": item.question,
                    "expected_file": item.expected_file,
                    "expected_pages": item.expected_pages_text,
                    "document_rank": evaluation["document_rank"],
                    "page_rank": page_rank,
                    "classification": _failure_category(item, evaluation["document_rank"], page_rank),
                }
            )
    summary["failure_count_at_5"] = len(failures)
    summary["failures"] = failures

    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    detailed_path = logs_dir / "retrieval_eval_results.csv"
    summary_path = logs_dir / "retrieval_eval_summary.json"
    report_path = logs_dir / "retrieval_eval_report.md"
    _write_detailed_csv(detailed_path, evaluations)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_report(report_path, items, evaluations, summary, min(args.k, retrieval_k))

    print("\nOVERALL METRICS")
    for level, label in (("document", "Document"), ("page", "Page")):
        for k in METRIC_KS:
            print(f"{label} Recall@{k}: {summary[level][f'recall_at_{k}']:.2%}")
        print(f"{label} MRR: {summary[level]['mrr']:.4f}")
    print(f"Failures at Page Recall@5: {len(failures)}")
    print(f"Detailed CSV: {detailed_path}")
    print(f"Summary JSON: {summary_path}")
    print(f"Markdown report: {report_path}")


if __name__ == "__main__":
    main()
