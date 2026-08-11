from __future__ import annotations

import argparse

from .db import Database
from .embedder import Embedder


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over Marathi GR chunks")
    parser.add_argument("query", type=str)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--department", type=str, default=None)
    args = parser.parse_args()

    embedder = Embedder()
    db = Database()
    query_vector = embedder.encode([args.query])[0]
    results = db.dense_search(query_vector, top_k=args.top_k, department=args.department)

    if not results:
        print("No results found.")
        return

    for rank, row in enumerate(results, start=1):
        date_value = row["gr_date"].isoformat() if row["gr_date"] else "unknown"
        print("=" * 100)
        print(f"Rank       : {rank}")
        print(f"Similarity : {float(row['similarity']):.4f}")
        print(f"Department : {row['department']}")
        print(f"GR number  : {row['gr_number'] or 'unknown'}")
        print(f"GR date    : {date_value}")
        print(f"Subject    : {row['subject'] or 'unknown'}")
        print(f"File       : {row['file_path']}")
        print(f"Pages      : {row['page_start']} - {row['page_end']}")
        print("-" * 100)
        print(row["text"][:1800])
        print()


if __name__ == "__main__":
    main()
