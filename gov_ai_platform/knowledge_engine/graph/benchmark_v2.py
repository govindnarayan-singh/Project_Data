from __future__ import annotations

import json
from typing import Any

from .repository import GraphRepository
from .traversal import GraphTraversalService


LABEL_SOURCE = "AUTO_VERIFIED_SOURCE_EVIDENCE"
FIELDS = (
    "benchmark_id", "query_type", "source_document_id", "expected_targets",
    "expected_relations", "expected_path", "expected_evidence_pages", "max_depth",
    "label_source", "notes",
)


def build_benchmark(repository: GraphRepository | None = None, *, target_size: int = 40) -> list[dict[str, Any]]:
    repository = repository or GraphRepository()
    with repository.connect() as connection:
        edges = [dict(row) for row in connection.execute(
            """SELECT r.id,r.source_document_id,r.target_document_id,r.relation_type,
                      min(c.source_page) evidence_page
                 FROM kg_relations r JOIN kg_relation_evidence e ON e.relation_id=r.id
                 JOIN kg_reference_candidates c ON c.id=e.candidate_id
                WHERE r.target_document_id IS NOT NULL
                  AND r.validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED')
                GROUP BY r.id ORDER BY r.id"""
        )]
        issued = [dict(row) for row in connection.execute(
            """SELECT r.id,r.source_document_id,r.target_department_id,d.canonical_name
                 FROM kg_relations r JOIN kg_departments d ON d.id=r.target_department_id
                WHERE r.relation_type='ISSUED_BY' ORDER BY r.id LIMIT 8"""
        )]
        document_ids = [int(row["id"]) for row in connection.execute("SELECT id FROM documents ORDER BY id")]
    rows: list[dict[str, Any]] = []
    def add(kind: str, source: int, targets: list[int | str], relations: list[str], path: list[int], pages: list[int], depth: int, note: str):
        rows.append({"benchmark_id": f"KGB2-{len(rows)+1:03d}", "query_type": kind,
                     "source_document_id": source, "expected_targets": json.dumps(targets),
                     "expected_relations": json.dumps(relations), "expected_path": json.dumps(path),
                     "expected_evidence_pages": json.dumps(pages), "max_depth": depth,
                     "label_source": LABEL_SOURCE, "notes": note})
    for edge in edges[:12]:
        add("DIRECT_REFERENCE" if edge["relation_type"] == "REFERENCES" else "OUTGOING_RELATION",
            int(edge["source_document_id"]), [int(edge["target_document_id"])], [edge["relation_type"]],
            [int(edge["source_document_id"]), int(edge["target_document_id"])], [int(edge["evidence_page"])], 1,
            f"Stored validated edge {edge['id']}")
    for edge in edges[:12]:
        add("REVERSE_REFERENCE" if edge["relation_type"] == "REFERENCES" else "INCOMING_RELATION",
            int(edge["target_document_id"]), [int(edge["source_document_id"])], [edge["relation_type"]],
            [int(edge["source_document_id"]), int(edge["target_document_id"])], [int(edge["evidence_page"])], 1,
            f"Reverse lookup of stored validated edge {edge['id']}")
    for edge in edges[12:18]:
        add("RELATION_EVIDENCE_PAGE", int(edge["source_document_id"]), [int(edge["target_document_id"])],
            [edge["relation_type"]], [int(edge["source_document_id"]), int(edge["target_document_id"])],
            [int(edge["evidence_page"])], 1, f"Evidence attached to relation {edge['id']}")
    for row in issued:
        add("ISSUED_BY", int(row["source_document_id"]), [str(row["canonical_name"])], ["ISSUED_BY"], [], [], 1,
            f"Stored department edge {row['id']}")
    # Evidence-based no-path cases use an isolated target and state the explicit bound.
    with repository.connect() as connection:
        isolated = connection.execute(
            """SELECT d.id FROM documents d WHERE NOT EXISTS
                 (SELECT 1 FROM kg_relations r WHERE r.target_document_id IS NOT NULL
                    AND (r.source_document_id=d.id OR r.target_document_id=d.id)) ORDER BY d.id LIMIT 1"""
        ).fetchone()
    if isolated and edges and len(rows) < target_size:
        source = int(edges[0]["source_document_id"]); target = int(isolated["id"])
        add("NO_PATH_WITHIN_BOUND", source, [target], [], [], [], 3,
            "Deterministically isolated target; expectation applies only within max depth")
    # Real directed multi-hop and shortest-path cases only.
    service = GraphTraversalService(repository)
    seen_paths: set[tuple[int, ...]] = set()
    for source in document_ids:
        for item in service.upstream(source, 3).paths:
            path = tuple(item["document_ids"])
            if item["cycle"] or len(path) not in {3, 4} or path in seen_paths:
                continue
            seen_paths.add(path)
            path_kind = "SHORTEST_PATH" if not any(row["query_type"] == "SHORTEST_PATH" for row in rows) else (
                "TWO_HOP_REFERENCE_CHAIN" if len(path) == 3 else "THREE_HOP_REFERENCE_CHAIN"
            )
            add(path_kind, source, [path[-1]], [hop["relation_type"] for hop in item["hops"]], list(path),
                [evidence["source_page"] for hop in item["hops"] for evidence in hop["evidence"][:1]], len(path) - 1,
                "Observed validated shortest directed chain" if path_kind == "SHORTEST_PATH" else "Observed validated directed chain")
            if len(rows) >= target_size:
                break
        if len(rows) >= target_size:
            break
    return rows[:target_size]


def evaluate_benchmark(rows: list[dict[str, Any]], repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository(); service = GraphTraversalService(repository)
    results = []
    for row in rows:
        kind = row["query_type"]; source = int(row["source_document_id"]); depth = int(row["max_depth"])
        targets = json.loads(row["expected_targets"]); relations = json.loads(row["expected_relations"])
        expected_path = json.loads(row["expected_path"]); pages = json.loads(row["expected_evidence_pages"])
        direct = reverse = path_ok = relation_ok = evidence_ok = provenance_ok = no_path_ok = None
        if kind == "ISSUED_BY":
            actual = repository.get_issued_by(source)
            direct = bool(actual and actual["canonical_name"] in targets)
            provenance_ok = bool(actual and actual["relation_id"])
        elif kind in {"REVERSE_REFERENCE", "INCOMING_RELATION"}:
            actual = service.downstream(source, 1).paths
            match = next((item for item in actual if item["end_document_id"] in targets), None)
            reverse = match is not None; relation_ok = bool(match and match["hops"][0]["relation_type"] in relations)
            evidence_ok = bool(match and any(e["source_page"] in pages for e in match["hops"][0]["evidence"]))
            provenance_ok = bool(match and match["hops"][0]["provenance_complete"])
        elif kind == "NO_PATH_WITHIN_BOUND":
            actual = service.paths(source, int(targets[0]), depth); no_path_ok = actual["status"] == "NO_PATH"; provenance_ok = True
        else:
            target = int(targets[0]); actual = service.paths(source, target, depth)
            match = actual if actual["status"] == "PATH_FOUND" else None
            if kind in {"DIRECT_REFERENCE", "OUTGOING_RELATION", "RELATION_EVIDENCE_PAGE"}: direct = bool(match and match["path_length"] == 1)
            else: path_ok = bool(match and match["document_ids"] == expected_path)
            relation_ok = bool(match and [hop["relation_type"] for hop in match["hops"]] == relations)
            actual_pages = [e["source_page"] for hop in match["hops"] for e in hop["evidence"]] if match else []
            evidence_ok = all(page in actual_pages for page in pages)
            provenance_ok = bool(match and all(hop["provenance_complete"] for hop in match["hops"]))
        checks = [value for value in (direct, reverse, path_ok, relation_ok, evidence_ok, provenance_ok, no_path_ok) if value is not None]
        results.append({"benchmark_id": row["benchmark_id"], "query_type": kind, "passed": all(checks),
                        "direct": direct, "reverse": reverse, "path": path_ok, "relations": relation_ok,
                        "evidence_page": evidence_ok, "provenance": provenance_ok, "no_path": no_path_ok})
    def metric(field: str):
        values = [row[field] for row in results if row[field] is not None]
        return {"correct": sum(values), "total": len(values), "accuracy": sum(values) / len(values) if values else None}
    return {"status": "PASS" if all(row["passed"] for row in results) else "FAIL", "rows": len(rows),
            "label_source": LABEL_SOURCE, "independent_human_ground_truth": False,
            "direct_edge_accuracy": metric("direct"), "reverse_edge_accuracy": metric("reverse"),
            "path_accuracy": metric("path"), "path_relation_accuracy": metric("relations"),
            "evidence_page_accuracy": metric("evidence_page"), "traversal_completeness": metric("path"),
            "no_path_accuracy": metric("no_path"), "provenance_completeness": metric("provenance"),
            "cases": results}


__all__ = ["FIELDS", "LABEL_SOURCE", "build_benchmark", "evaluate_benchmark"]
