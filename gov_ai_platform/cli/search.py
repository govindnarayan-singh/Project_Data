from __future__ import annotations

import argparse
import sys

from knowledge_engine.retrieval.service import ALLOWED_TOP_K, RetrievalService


def _preview(text: str, limit: int = 600) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit - 1].rstrip() + "…"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Semantic evidence search over Marathi GR chunks")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, choices=sorted(ALLOWED_TOP_K), default=5)
    parser.add_argument("--department")
    args = parser.parse_args()
    results = RetrievalService().search(args.query, top_k=args.top_k, department=args.department)
    if not results:
        print("No matching GR passages were found.")
        return
    for result in results:
        print("=" * 100)
        print(f"Rank       : {result.rank}")
        print(f"Similarity : {result.similarity:.4f}")
        print(f"Department : {result.department}")
        print(f"GR number  : {result.gr_number or 'Unknown'}")
        print(f"GR date    : {result.gr_date.isoformat() if result.gr_date else 'Unknown'}")
        print(f"Subject    : {result.subject or 'Unknown'}")
        print(f"File       : {result.file_name}")
        print(f"Pages      : {result.page_start}-{result.page_end}")
        print(f"Passage    : {_preview(result.text)}")


if __name__ == "__main__":
    main()
