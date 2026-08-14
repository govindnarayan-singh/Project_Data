from __future__ import annotations

import csv
from pathlib import Path

from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.settings import PROJECT_ROOT


OUTPUT_PATH = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v1.csv"
FIELDS = (
    "source_document",
    "target_document",
    "relation_type",
    "source_page",
    "evidence",
    "expected_resolution",
    "label_source",
)


def run(path: Path = OUTPUT_PATH) -> list[dict[str, str | int]]:
    repository = GraphRepository()
    rows: list[dict[str, str | int]] = []
    for edge in repository.all_edges():
        if edge["relation_type"] == "ISSUED_BY" or not edge.get("target_document_id"):
            continue
        for evidence in repository.get_relation_evidence(int(edge["id"])):
            rows.append(
                {
                    "source_document": edge["source_file"],
                    "target_document": edge["target_file"],
                    "relation_type": edge["relation_type"],
                    "source_page": evidence["source_page"],
                    "evidence": evidence["raw_evidence_text"],
                    "expected_resolution": f"document_id={edge['target_document_id']}",
                    "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE",
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> None:
    rows = run()
    print(f"Benchmark evidence rows: {len(rows)}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
