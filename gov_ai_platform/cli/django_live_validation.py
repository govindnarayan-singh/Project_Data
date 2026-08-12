from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def _database_fingerprint(connection) -> dict[str, object]:
    counts = connection.execute(
        """SELECT (SELECT count(*) FROM documents) AS documents,
                  (SELECT count(*) FROM pages) AS pages,
                  (SELECT count(*) FROM chunks) AS chunks,
                  (SELECT count(embedding) FROM chunks) AS embeddings"""
    ).fetchone()
    identities = connection.execute(
        "SELECT id,file_path,sha256 FROM documents ORDER BY id"
    ).fetchall()
    digest = hashlib.sha256()
    for row in identities:
        digest.update(f"{row['id']}|{row['file_path']}|{row['sha256']}\n".encode("utf-8"))
    return {
        **{key: int(value) for key, value in counts.items()},
        "document_identity_sha256": digest.hexdigest(),
    }


def run(manifest_path: Path, smoke_path: Path, output_path: Path) -> dict[str, object]:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    import psycopg
    from django.test import Client
    from psycopg.rows import dict_row

    from knowledge_engine.settings import settings

    django.setup()
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        manifest = list(csv.DictReader(handle))
    with smoke_path.open("r", encoding="utf-8-sig", newline="") as handle:
        smoke = list(csv.DictReader(handle))
    paths = [str(Path(row["file_path"])) for row in manifest]

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        before = _database_fingerprint(connection)
        documents = list(
            connection.execute(
                "SELECT id,file_name,file_path FROM documents WHERE file_path = ANY(%s) ORDER BY id",
                (paths,),
            ).fetchall()
        )

    client = Client(HTTP_HOST="localhost")
    search_checks = []
    for row in smoke[:5]:
        response = client.post("/", {"query": row["query"], "top_k": "10"})
        body = response.content.decode("utf-8", errors="replace")
        search_checks.append(
            {
                "source_pdf": row["source_pdf"],
                "status_code": response.status_code,
                "new_pdf_present_in_top_k_html": row["source_pdf"] in body,
                "response_bytes": len(response.content),
            }
        )

    pdf_checks = []
    smoke_names = {row["source_pdf"] for row in smoke}
    selected_documents = [row for row in documents if row["file_name"] in smoke_names][:10]
    if len(selected_documents) < 10:
        selected_ids = {int(row["id"]) for row in selected_documents}
        selected_documents.extend(
            row for row in documents if int(row["id"]) not in selected_ids
        )
        selected_documents = selected_documents[:10]
    for document in selected_documents:
        response = client.get(f"/documents/{document['id']}/pdf/")
        first_chunk = b""
        if response.status_code == 200:
            iterator = iter(response.streaming_content)
            first_chunk = next(iterator, b"")
            response.close()
        pdf_checks.append(
            {
                "document_id": int(document["id"]),
                "file_name": document["file_name"],
                "status_code": response.status_code,
                "content_type": response.get("Content-Type", ""),
                "pdf_signature": first_chunk.startswith(b"%PDF"),
            }
        )

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        after = _database_fingerprint(connection)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "search_requests": search_checks,
        "pdf_viewer_requests": pdf_checks,
        "top_k_returns_new_pdfs": all(
            row["status_code"] == 200 and row["new_pdf_present_in_top_k_html"]
            for row in search_checks
        ),
        "ten_new_pdf_viewers_pass": len(pdf_checks) == 10
        and all(
            row["status_code"] == 200
            and row["content_type"] == "application/pdf"
            and row["pdf_signature"]
            for row in pdf_checks
        ),
        "database_before": before,
        "database_after": after,
        "search_requests_read_only": before == after,
    }
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Validate live Django search and PDF delivery")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("smoke_tests", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.manifest.resolve(), args.smoke_tests.resolve(), args.output.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
