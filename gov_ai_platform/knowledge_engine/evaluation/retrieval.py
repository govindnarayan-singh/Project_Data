from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from knowledge_engine.retrieval.service import RetrievalService
from knowledge_engine.settings import PROJECT_ROOT, settings


METRIC_KS = (1, 3, 5, 10)
REQUIRED_COLUMNS = {"question_id", "question", "expected_file", "expected_gr_number", "expected_pages", "department", "notes"}


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


def is_document_hit(result: Any, expected_file: str) -> bool:
    value = result["file_name"] if isinstance(result, dict) else result.file_name
    return value == expected_file


def is_page_hit(result: Any, expected_file: str, expected_pages: set[int]) -> bool:
    start = result["page_start"] if isinstance(result, dict) else result.page_start
    end = result["page_end"] if isinstance(result, dict) else result.page_end
    return is_document_hit(result, expected_file) and page_range_overlaps(int(start), int(end), expected_pages)


def first_matching_rank(matches: Iterable[bool]) -> int | None:
    return next((rank for rank, matched in enumerate(matches, start=1) if matched), None)


def recall_at_k(ranks: Iterable[int | None], k: int) -> float:
    values = list(ranks)
    return 0.0 if not values else sum(rank is not None and rank <= k for rank in values) / len(values)


def reciprocal_rank(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / rank


def mean_reciprocal_rank(ranks: Iterable[int | None]) -> float:
    values = list(ranks)
    return 0.0 if not values else sum(reciprocal_rank(rank) for rank in values) / len(values)


def aggregate_results(document_ranks: Iterable[int | None], page_ranks: Iterable[int | None], ks: Iterable[int] = METRIC_KS) -> dict[str, Any]:
    document_values, page_values = list(document_ranks), list(page_ranks)
    if len(document_values) != len(page_values):
        raise ValueError("Document and page rank lists must have the same length")
    return {
        "question_count": len(document_values),
        "document": {**{f"recall_at_{k}": recall_at_k(document_values, k) for k in ks}, "mrr": mean_reciprocal_rank(document_values)},
        "page": {**{f"recall_at_{k}": recall_at_k(page_values, k) for k in ks}, "mrr": mean_reciprocal_rank(page_values)},
    }


def load_dataset(path: Path) -> list[EvaluationItem]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Evaluation CSV is missing columns: {sorted(missing)}")
        rows = list(reader)
    items = []
    for row in rows:
        values = {key: (row.get(key) or "").strip() for key in REQUIRED_COLUMNS}
        items.append(EvaluationItem(values["question_id"], values["question"], values["expected_file"], values["expected_gr_number"], values["expected_pages"], frozenset(parse_expected_pages(values["expected_pages"])), values["department"], values["notes"]))
    return items


def evaluate(path: Path, service: RetrievalService | None = None) -> dict[str, Any]:
    service = service or RetrievalService()
    items = load_dataset(path)
    document_ranks: list[int | None] = []
    page_ranks: list[int | None] = []
    for item in items:
        results = service.search(item.question, top_k=10)
        document_ranks.append(first_matching_rank(is_document_hit(result, item.expected_file) for result in results))
        page_ranks.append(first_matching_rank(is_page_hit(result, item.expected_file, set(item.expected_pages)) for result in results))
    summary = aggregate_results(document_ranks, page_ranks)
    summary.update({"evaluation_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), "model": settings.embed_model, "embedding_dimension": settings.embed_dim})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate dense retrieval against frozen page-level labels")
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    summary = evaluate(args.csv_path)
    output_path = PROJECT_ROOT / "outputs" / "retrieval_eval_summary.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Document Recall@1: {summary['document']['recall_at_1']:.2%}")
    print(f"Page Recall@5: {summary['page']['recall_at_5']:.2%}")
    print(f"Page Recall@10: {summary['page']['recall_at_10']:.2%}")


if __name__ == "__main__":
    main()
