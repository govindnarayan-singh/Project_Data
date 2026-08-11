from __future__ import annotations

import argparse

from .db import Database
from .embedder import Embedder


def _preview(text: str, limit: int = 600) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def main() -> None:
    parser = argparse.ArgumentParser(description="Client-friendly Marathi GR semantic search demo")
    parser.add_argument("question")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if args.top_k <= 0:
        parser.error("--top-k must be positive")

    vector = Embedder().encode([args.question])[0]
    results = Database().dense_search(vector, top_k=args.top_k)

    print("QUESTION")
    print(args.question)
    print("\nTOP RESULTS")
    if not results:
        print("No matching GR passages were found.")
        return

    for rank, row in enumerate(results, start=1):
        date_value = row["gr_date"].isoformat() if row["gr_date"] else "Unknown"
        print(f"\n{rank}. GR: {row['gr_number'] or 'Unknown'}")
        print(f"   Department: {row['department']}")
        print(f"   Date: {date_value}")
        print(f"   File: {row['file_name']}")
        print(f"   Pages: {row['page_start']}-{row['page_end']}")
        print(f"   Similarity: {float(row['similarity']):.4f}")
        print(f"   Relevant passage: {_preview(row['text'])}")


if __name__ == "__main__":
    main()
