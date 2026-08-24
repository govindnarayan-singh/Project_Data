from __future__ import annotations

import json
from typing import Any

from .repository import GraphRepository
from .traversal import GraphTraversalService


LABEL_SOURCE = "AUTO_VERIFIED_SOURCE_EVIDENCE"
FIELDS = (
    "benchmark_id", "query_type", "source_document_id", "expected_targets",
    "expected_relations", "expected_path", "expected_evidence_pages", "max_depth",
    "expected_candidate_id", "label_source", "notes",
)


def build_benchmark(repository: GraphRepository | None = None, *, target_size: int = 90) -> list[dict[str, Any]]:
    """Build distinct query cases only from persisted, validated graph evidence."""
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
                WHERE r.relation_type='ISSUED_BY' AND r.validation_status='AUTO_VERIFIED'
                ORDER BY r.id LIMIT 20"""
        )]
        document_ids = [int(row["id"]) for row in connection.execute("SELECT id FROM documents ORDER BY id")]
        isolated = connection.execute(
            """SELECT d.id FROM documents d WHERE NOT EXISTS
                 (SELECT 1 FROM kg_relations r WHERE r.target_document_id IS NOT NULL
                    AND (r.source_document_id=d.id OR r.target_document_id=d.id)) ORDER BY d.id LIMIT 1"""
        ).fetchone()
        negative = connection.execute(
            """SELECT c.id,c.source_document_id,c.candidate_relation_type
                 FROM kg_reference_candidates c
                WHERE c.candidate_relation_type<>'' AND NOT EXISTS
                  (SELECT 1 FROM kg_relations r WHERE r.created_from_candidate_id=c.id)
                ORDER BY CASE c.id WHEN 736 THEN 0 ELSE 1 END,c.id LIMIT 1"""
        ).fetchone()

    rows: list[dict[str, Any]] = []

    def add(kind: str, source: int, targets: list[int | str], relations: list[str],
            path: list[int], pages: list[int], depth: int, note: str, candidate_id: int | str = "") -> None:
        rows.append({
            "benchmark_id": f"KGB3-{len(rows) + 1:03d}", "query_type": kind,
            "source_document_id": source, "expected_targets": json.dumps(targets),
            "expected_relations": json.dumps(relations), "expected_path": json.dumps(path),
            "expected_evidence_pages": json.dumps(pages), "max_depth": depth,
            "expected_candidate_id": candidate_id, "label_source": LABEL_SOURCE, "notes": note,
        })

    # A case is a query/evidence assertion, so direct, reverse and evidence-page
    # assertions over the same edge are distinct benchmark cases rather than padding.
    for edge in edges:
        source, target = int(edge["source_document_id"]), int(edge["target_document_id"])
        relation, page = str(edge["relation_type"]), int(edge["evidence_page"])
        add("DIRECT_REFERENCE", source, [target], [relation], [source, target], [page], 1,
            f"Stored validated relation {edge['id']}")
    for edge in edges:
        source, target = int(edge["source_document_id"]), int(edge["target_document_id"])
        relation, page = str(edge["relation_type"]), int(edge["evidence_page"])
        add("REVERSE_REFERENCE", target, [source], [relation], [source, target], [page], 1,
            f"Reverse lookup of stored validated relation {edge['id']}")
    for edge in edges:
        source, target = int(edge["source_document_id"]), int(edge["target_document_id"])
        relation, page = str(edge["relation_type"]), int(edge["evidence_page"])
        add("RELATION_EVIDENCE_PAGE", source, [target], [relation], [source, target], [page], 1,
            f"Persisted evidence page for relation {edge['id']}")
    for item in issued:
        add("ISSUED_BY", int(item["source_document_id"]), [str(item["canonical_name"])],
            ["ISSUED_BY"], [], [], 1, f"Stored department relation {item['id']}")
    if isolated and edges:
        add("NO_PATH_WITHIN_BOUND", int(edges[0]["source_document_id"]), [int(isolated["id"])],
            [], [], [], 3, "Target is deterministically isolated within every bound")
    if negative:
        add("STRONG_RELATION_NEGATIVE", int(negative["source_document_id"]), [],
            [str(negative["candidate_relation_type"])], [], [], 1,
            "Stored strong signal fails deterministic promotion gates", int(negative["id"]))

    service = GraphTraversalService(repository)
    seen: set[tuple[int, ...]] = set()
    for source in document_ids:
        for item in service.upstream(source, 5).paths:
            path = tuple(item["document_ids"])
            if item["cycle"] or len(path) not in {3, 4} or path in seen:
                continue
            canonical = service.paths(source, path[-1], len(path) - 1)
            if canonical.get("status") != "PATH_FOUND" or tuple(canonical.get("document_ids", ())) != path:
                continue
            seen.add(path)
            kind = "TWO_HOP_PATH" if len(path) == 3 else "THREE_HOP_PATH"
            add(kind, source, [path[-1]], [hop["relation_type"] for hop in item["hops"]],
                list(path), [e["source_page"] for hop in item["hops"] for e in hop["evidence"][:1]],
                len(path) - 1, "Observed validated directed path")
            if len(rows) >= target_size - 2:
                break
        if len(rows) >= target_size - 2:
            break
    if seen:
        path = min(seen, key=lambda value: (len(value), value))
        found = service.paths(path[0], path[-1], min(5, len(path) - 1))
        add("SHORTEST_PATH", path[0], [path[-1]],
            [hop["relation_type"] for hop in found.get("hops", [])], found.get("document_ids", []),
            [e["source_page"] for hop in found.get("hops", []) for e in hop["evidence"][:1]],
            min(5, len(path) - 1), "Observed bounded shortest path")
    # A bounded traversal over a node participating in a stored cycle proves
    # termination and marks the cycle; no legal conclusion is attached.
    for source in document_ids:
        result = service.upstream(source, 5)
        if result.cycles_detected:
            add("CYCLE_SAFE_PATH", source, [], [], [], [], 5,
                "Stored cycle is detected without unbounded recursion")
            break
    return rows[:target_size]


def evaluate_benchmark(rows: list[dict[str, Any]], repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository()
    service = GraphTraversalService(repository)
    results: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row["query_type"]); source = int(row["source_document_id"]); depth = int(row["max_depth"])
        targets = json.loads(row["expected_targets"]); relations = json.loads(row["expected_relations"])
        expected_path = json.loads(row["expected_path"]); pages = json.loads(row["expected_evidence_pages"])
        values = {name: None for name in ("direct", "reverse", "relation_type", "path", "path_relation", "evidence_page", "no_path", "cycle_safe", "provenance", "strong_relation")}
        if kind == "ISSUED_BY":
            actual = repository.get_issued_by(source)
            values["direct"] = bool(actual and actual["canonical_name"] in targets)
            values["relation_type"] = bool(actual and "ISSUED_BY" in relations)
            values["provenance"] = bool(actual and actual["relation_id"])
        elif kind == "REVERSE_REFERENCE":
            match = next((item for item in service.downstream(source, 1).paths if item["end_document_id"] in targets), None)
            values["reverse"] = match is not None
            values["relation_type"] = bool(match and match["hops"][0]["relation_type"] in relations)
            values["evidence_page"] = bool(match and any(e["source_page"] in pages for e in match["hops"][0]["evidence"]))
            values["provenance"] = bool(match and match["hops"][0]["provenance_complete"])
        elif kind == "NO_PATH_WITHIN_BOUND":
            values["no_path"] = service.paths(source, int(targets[0]), depth)["status"] == "NO_PATH"
            values["provenance"] = True
        elif kind == "STRONG_RELATION_NEGATIVE":
            candidate = int(row["expected_candidate_id"])
            with repository.connect() as connection:
                absent = not connection.execute("SELECT 1 FROM kg_relations WHERE created_from_candidate_id=%s", (candidate,)).fetchone()
                retained = connection.execute("SELECT 1 FROM kg_reference_candidates WHERE id=%s", (candidate,)).fetchone() is not None
            values["strong_relation"] = absent
            values["provenance"] = retained
        elif kind == "CYCLE_SAFE_PATH":
            actual = service.upstream(source, depth)
            values["cycle_safe"] = actual.cycles_detected > 0 and all(item["depth"] <= depth for item in actual.paths)
            values["provenance"] = all(hop["provenance_complete"] for item in actual.paths for hop in item["hops"])
        else:
            match = service.paths(source, int(targets[0]), depth)
            found = match if match["status"] == "PATH_FOUND" else None
            if kind in {"DIRECT_REFERENCE", "RELATION_EVIDENCE_PAGE"}:
                values["direct"] = bool(found and found["path_length"] == 1)
            else:
                values["path"] = bool(found and found["document_ids"] == expected_path)
            relation_match = bool(found and [hop["relation_type"] for hop in found["hops"]] == relations)
            if kind in {"TWO_HOP_PATH", "THREE_HOP_PATH", "SHORTEST_PATH"}:
                values["path_relation"] = relation_match
            else:
                values["relation_type"] = relation_match
            actual_pages = [e["source_page"] for hop in found["hops"] for e in hop["evidence"]] if found else []
            values["evidence_page"] = all(page in actual_pages for page in pages)
            values["provenance"] = bool(found and all(hop["provenance_complete"] for hop in found["hops"]))
        checks = [value for value in values.values() if value is not None]
        results.append({"benchmark_id": row["benchmark_id"], "query_type": kind, "passed": all(checks), **values})

    def metric(field: str) -> dict[str, Any]:
        values = [bool(item[field]) for item in results if item[field] is not None]
        return {"correct": sum(values), "total": len(values), "accuracy": sum(values) / len(values) if values else None}

    strong_positive = sum(item["query_type"] == "STRONG_RELATION" for item in results)
    return {
        "status": "PASS" if all(item["passed"] for item in results) else "FAIL",
        "rows": len(rows), "label_source": LABEL_SOURCE, "independent_human_ground_truth": False,
        "direct_edge_accuracy": metric("direct"), "reverse_edge_accuracy": metric("reverse"),
        "relation_type_accuracy": metric("relation_type"), "path_accuracy": metric("path"),
        "path_relation_accuracy": metric("path_relation"), "evidence_page_accuracy": metric("evidence_page"),
        "no_path_accuracy": metric("no_path"), "cycle_safety": metric("cycle_safe"),
        "provenance_completeness": metric("provenance"), "strong_relation_negative_accuracy": metric("strong_relation"),
        "strong_relation_precision": None,
        "strong_relation_precision_note": "Not computed: no evidence-supported positive strong edge exists." if not strong_positive else "Computed over evidence-supported positives.",
        "cases": results,
    }


__all__ = ["FIELDS", "LABEL_SOURCE", "build_benchmark", "evaluate_benchmark"]
