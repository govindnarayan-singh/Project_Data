from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .db import Database
from .embedder import Embedder


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval Recall@K using a gold CSV")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    rows = list(csv.DictReader(args.csv_path.open("r", encoding="utf-8-sig")))
    if not rows:
        raise ValueError("Evaluation CSV is empty")

    embedder = Embedder()
    db = Database()

    hits = 0
    for i, item in enumerate(rows, start=1):
        query = item["query"].strip()
        expected_file = item["expected_file"].strip()
        department = item.get("department", "").strip() or None

        vector = embedder.encode([query])[0]
        results = db.dense_search(vector, top_k=args.k, department=department)
        returned = {r["file_path"] for r in results}
        hit = expected_file in returned
        hits += int(hit)
        print(f"{i:03d} | {'HIT ' if hit else 'MISS'} | {query}")
        if not hit:
            print(f"      expected: {expected_file}")
            print(f"      got     : {[r['file_path'] for r in results]}")

    recall = hits / len(rows)
    print(f"\nRecall@{args.k}: {hits}/{len(rows)} = {recall:.3f}")


if __name__ == "__main__":
    main()
