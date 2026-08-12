from __future__ import annotations

import json
import os
from datetime import datetime


def run() -> dict[str, object]:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()

    from django.test import Client

    from knowledge_engine.evaluation.retrieval import evaluate, load_dataset
    from knowledge_engine.portal import PortalRepository
    from knowledge_engine.retrieval.service import RetrievalService
    from knowledge_engine.settings import PROJECT_ROOT

    repository = PortalRepository()
    with repository.connect() as connection:
        before = dict(connection.execute(
            """SELECT (SELECT count(*) FROM documents) documents,
                      (SELECT count(*) FROM pages) pages,
                      (SELECT count(*) FROM chunks) chunks,
                      (SELECT count(*) FROM kg_reference_candidates) candidates,
                      (SELECT count(*) FROM kg_relations) relations"""
        ).fetchone())
        documents = [dict(row) for row in connection.execute(
            "SELECT id,file_name FROM documents ORDER BY id LIMIT 5"
        ).fetchall()]
        graph_documents = [int(row["source_document_id"]) for row in connection.execute(
            "SELECT DISTINCT source_document_id FROM kg_reference_candidates ORDER BY source_document_id LIMIT 3"
        ).fetchall()]

    dataset_path = PROJECT_ROOT / "data" / "retrieval_eval_10pdf.csv"
    items = load_dataset(dataset_path)
    client = Client(HTTP_HOST="localhost")
    search_checks = []
    for item in items[:5]:
        response = client.post("/", {"query": item.question, "top_k": "10", "department": ""})
        search_checks.append({"question_id": item.question_id, "status": response.status_code,
                              "expected_file": item.expected_file,
                              "expected_file_present": item.expected_file in response.content.decode("utf-8", "replace")})

    document_checks = []
    pdf_checks = []
    for document in documents:
        tabs = {}
        for tab in ("overview", "evidence", "pdf", "relationships", "metadata"):
            tabs[tab] = client.get(f"/documents/{document['id']}/?tab={tab}").status_code
        document_checks.append({"id": int(document["id"]), "file_name": document["file_name"], "tabs": tabs})
        response = client.get(f"/documents/{document['id']}/pdf/")
        first = next(iter(response.streaming_content), b"") if response.status_code == 200 else b""
        response.close()
        pdf_checks.append({"id": int(document["id"]), "status": response.status_code,
                           "content_type": response.get("Content-Type", ""), "pdf_signature": first.startswith(b"%PDF")})

    graph_checks = [{"id": document_id, "status": client.get(
        f"/documents/{document_id}/?tab=relationships").status_code} for document_id in graph_documents]
    workspace_checks = {path: client.get(path).status_code for path in (
        "/documents/", "/knowledge-graph/", "/knowledge-graph/unresolved/",
        "/system/corpus/", "/system/evaluation/",
    )}

    benchmark = evaluate(dataset_path, service=RetrievalService())
    expected = json.loads((PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "retrieval_regression.json").read_text(encoding="utf-8"))["after"]
    benchmark_unchanged = all(
        abs(float(benchmark[level][metric]) - float(expected[level][metric])) < 1e-12
        for level in ("document", "page") for metric in expected[level]
    ) and benchmark["question_count"] == expected["question_count"]

    with repository.connect() as connection:
        after = dict(connection.execute(
            """SELECT (SELECT count(*) FROM documents) documents,
                      (SELECT count(*) FROM pages) pages,
                      (SELECT count(*) FROM chunks) chunks,
                      (SELECT count(*) FROM kg_reference_candidates) candidates,
                      (SELECT count(*) FROM kg_relations) relations"""
        ).fetchone())

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "search_checks": search_checks, "document_checks": document_checks,
        "pdf_checks": pdf_checks, "graph_document_checks": graph_checks,
        "workspace_checks": workspace_checks, "benchmark": benchmark,
        "benchmark_unchanged": benchmark_unchanged,
        "database_before": before, "database_after": after, "read_only": before == after,
    }
    report["status"] = "PASS" if (
        all(item["status"] == 200 and item["expected_file_present"] for item in search_checks)
        and all(all(code == 200 for code in item["tabs"].values()) for item in document_checks)
        and all(item["status"] == 200 and item["content_type"] == "application/pdf" and item["pdf_signature"] for item in pdf_checks)
        and all(item["status"] == 200 for item in graph_checks)
        and all(code == 200 for code in workspace_checks.values())
        and benchmark_unchanged and before == after
    ) else "FAIL"
    output = PROJECT_ROOT / "outputs" / "ui_live_validation.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
