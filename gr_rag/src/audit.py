from __future__ import annotations

import argparse

from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit ingestion quality")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    db = Database()
    with db.connect() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS documents,
                COALESCE(SUM(page_count), 0) AS pages,
                COALESCE(SUM(digital_pages), 0) AS digital_pages,
                COALESCE(SUM(ocr_pages), 0) AS ocr_pages,
                COUNT(*) FILTER (WHERE gr_number IS NULL) AS missing_gr_number,
                COUNT(*) FILTER (WHERE gr_date IS NULL) AS missing_gr_date,
                COUNT(*) FILTER (WHERE subject IS NULL) AS missing_subject
            FROM documents
            """
        ).fetchone()

        print("Corpus audit")
        for key, value in totals.items():
            print(f"{key:20s}: {value}")

        print("\nLowest-quality extracted pages")
        rows = conn.execute(
            """
            SELECT d.file_path, p.page_number, p.extraction_method, p.quality_score,
                   left(p.text, 300) AS preview
            FROM pages p
            JOIN documents d ON d.id = p.document_id
            ORDER BY p.quality_score ASC NULLS FIRST
            LIMIT %s
            """,
            (args.limit,),
        ).fetchall()
        for r in rows:
            print("-" * 90)
            print(
                f"{r['file_path']} | page={r['page_number']} | "
                f"method={r['extraction_method']} | quality={r['quality_score']:.3f}"
            )
            print(r["preview"].replace("\n", " "))


if __name__ == "__main__":
    main()
