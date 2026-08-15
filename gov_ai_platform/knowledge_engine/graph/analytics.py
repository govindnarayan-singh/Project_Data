from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict
from time import perf_counter
from typing import Any

from knowledge_engine.external_acquisition.identity import legal_identity_quality

from .repository import GraphRepository
from .traversal import DOCUMENT_RELATIONS, GraphTraversalService
from .validation import KnowledgeGraphValidationPolicy, classify_relation_signal


STRONG_TYPES = ("AMENDS", "CORRECTS", "SUPERSEDES", "PARTIALLY_SUPERSEDES", "CANCELS", "EXTENDS")
ADAPTER_KINDS = frozenset({"ACT", "RULE", "NOTIFICATION", "GOVERNMENT_RESOLUTION", "GOVERNMENT_CIRCULAR", "CIRCULAR"})


def administrative_effects(document_id: int, repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository()
    with repository.connect() as connection:
        if not connection.execute("SELECT 1 FROM documents WHERE id=%s", (document_id,)).fetchone():
            raise ValueError(f"Document {document_id} does not exist")
        counts = {str(row["relation_type"]): int(row["count"]) for row in connection.execute(
            """SELECT relation_type,count(*) count FROM kg_relations
                WHERE target_document_id=%s AND validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED')
                GROUP BY relation_type""", (document_id,)
        )}
    return {
        "document_id": document_id,
        "HAS_INCOMING_AMENDMENT": counts.get("AMENDS", 0) > 0,
        "HAS_INCOMING_CORRECTION": counts.get("CORRECTS", 0) > 0,
        "HAS_INCOMING_SUPERSESSION": counts.get("SUPERSEDES", 0) + counts.get("PARTIALLY_SUPERSEDES", 0) > 0,
        "HAS_INCOMING_EXTENSION": counts.get("EXTENDS", 0) > 0,
        "HAS_INCOMING_CANCELLATION": counts.get("CANCELS", 0) > 0,
        "REFERENCED_BY_COUNT": counts.get("REFERENCES", 0),
        "derived_read_only": True,
        "legal_status_inferred": False,
    }


def relation_diagnostics(repository: GraphRepository | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    repository = repository or GraphRepository()
    candidates = repository.all_candidates()
    policy = KnowledgeGraphValidationPolicy("auto")
    inventory: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    phrase_counts: Counter[tuple[str, str, str]] = Counter()
    for row in candidates:
        relation_type = str(row.get("candidate_relation_type") or "")
        if not relation_type:
            continue
        assessment = classify_relation_signal(relation_type, str(row.get("raw_evidence_text") or ""))
        decision = policy.strong_relation_decision(row)
        phrase_counts[(relation_type, assessment.confidence.value, assessment.pattern_id)] += 1
        blockers = [] if decision.approved else [decision.reason]
        item = {
            "candidate_id": row["id"], "relation_type": relation_type,
            "source_document_id": row["source_document_id"], "source_document": row["source_file"],
            "source_page": row["source_page"], "raw_evidence": row["raw_evidence_text"],
            "target_identifier": row["normalized_document_number"],
            "target_document_id": row.get("resolved_target_document_id"),
            "target_resolution": row["resolution_status"], "phrase_family": assessment.pattern_id,
            "signal_class": assessment.confidence.value,
            "page_quality": row.get("source_quality_score"),
            "validation_blocker": "; ".join(blockers),
            "edge_decision": "AUTO_VERIFIED_EDGE" if decision.approved else "RETAIN_CANDIDATE",
            "decision_reason": decision.reason,
        }
        inventory.append(item)
        evaluations.append({**item, "machine_state": decision.validation_status, "approved": decision.approved})
    catalogue = [
        {"relation_type": relation_type, "classification": confidence, "phrase_family": pattern,
         "observed_signals": count,
         "promotion_semantics": "Eligible only when explicit wording links the action to the exact resolved target and every quality/provenance gate passes."}
        for (relation_type, confidence, pattern), count in sorted(phrase_counts.items())
    ]
    return inventory, evaluations, catalogue


def _components(document_ids: set[int], edges: list[dict[str, Any]]) -> tuple[dict[int, int], list[list[int]]]:
    adjacent: dict[int, set[int]] = defaultdict(set)
    for edge in edges:
        left, right = int(edge["source_document_id"]), int(edge["target_document_id"])
        adjacent[left].add(right); adjacent[right].add(left)
    seen: set[int] = set(); components: list[list[int]] = []
    for root in sorted(document_ids):
        if root in seen:
            continue
        queue = deque([root]); seen.add(root); component = []
        while queue:
            node = queue.popleft(); component.append(node)
            for neighbor in sorted(adjacent[node] - seen):
                seen.add(neighbor); queue.append(neighbor)
        components.append(sorted(component))
    components.sort(key=lambda values: (-len(values), values))
    mapping = {node: number for number, values in enumerate(components, 1) for node in values}
    return mapping, components


def _cycles(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjacent: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for edge in edges:
        adjacent[int(edge["source_document_id"])].append((int(edge["target_document_id"]), int(edge["id"])))
    found: dict[tuple[int, ...], dict[str, Any]] = {}
    def visit(start: int, node: int, nodes: list[int], relations: list[int]) -> None:
        if len(relations) >= 5:
            return
        for target, relation_id in adjacent[node]:
            if target == start:
                cycle_nodes = nodes + [start]
                body = cycle_nodes[:-1]
                rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
                key = min(rotations)
                found[key] = {"document_ids": cycle_nodes, "relation_ids": relations + [relation_id], "length": len(relations) + 1}
            elif target not in nodes:
                visit(start, target, nodes + [target], relations + [relation_id])
    for start in sorted(adjacent):
        visit(start, start, [start], [])
    return list(found.values())


def topology(repository: GraphRepository | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[int, int]]:
    repository = repository or GraphRepository()
    with repository.connect() as connection:
        documents = [dict(row) for row in connection.execute("SELECT id,file_name FROM documents ORDER BY id")]
        edges = [dict(row) for row in connection.execute(
            """SELECT id,source_document_id,target_document_id,relation_type,validation_status
                 FROM kg_relations WHERE target_document_id IS NOT NULL
                  AND validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED') ORDER BY id"""
        )]
    ids = {int(row["id"]) for row in documents}; names = {int(row["id"]): row["file_name"] for row in documents}
    mapping, components = _components(ids, edges)
    outgoing = Counter(int(row["source_document_id"]) for row in edges)
    incoming = Counter(int(row["target_document_id"]) for row in edges)
    reference_edges = [row for row in edges if row["relation_type"] == "REFERENCES"]
    ref_out = Counter(int(row["source_document_id"]) for row in reference_edges)
    ref_in = Counter(int(row["target_document_id"]) for row in reference_edges)
    participants = set(outgoing) | set(incoming); cycles = _cycles(edges)
    relation_counts = Counter(row["relation_type"] for row in edges)
    component_rows = [
        {"component_id": number, "size": len(values), "document_ids": ";".join(map(str, values)),
         "document_names": ";".join(str(names[value]) for value in values)}
        for number, values in enumerate(components, 1)
    ]
    metrics = {
        "total_documents": len(ids), "document_to_document_edges": len(edges),
        **{name: relation_counts.get(name, 0) for name in DOCUMENT_RELATIONS},
        "documents_with_outgoing_relations": len(outgoing), "documents_with_incoming_relations": len(incoming),
        "isolated_documents": len(ids - participants), "connected_documents": len(participants),
        "graph_document_coverage": {"numerator": len(participants), "denominator": len(ids),
                                    "percentage": len(participants) / len(ids) if ids else 0.0},
        "weakly_connected_components": len(components), "largest_component_size": max(map(len, components), default=0),
        "cycle_count": len(cycles), "maximum_observed_validated_traversal_depth": _maximum_depth(ids, edges),
        "average_outgoing_REFERENCES": len(reference_edges) / len(ids) if ids else 0.0,
        "average_incoming_REFERENCES": len(reference_edges) / len(ids) if ids else 0.0,
        "top_referenced_documents": [{"document_id": key, "file_name": names[key], "count": value} for key, value in ref_in.most_common(10)],
        "top_citing_documents": [{"document_id": key, "file_name": names[key], "count": value} for key, value in ref_out.most_common(10)],
        "issued_by_excluded": True,
    }
    return metrics, component_rows, cycles, mapping


def _maximum_depth(ids: set[int], edges: list[dict[str, Any]]) -> int:
    adjacent: dict[int, set[int]] = defaultdict(set)
    for row in edges:
        adjacent[int(row["source_document_id"])].add(int(row["target_document_id"]))
    maximum = 0
    for start in ids:
        queue = deque([(start, 0, frozenset({start}))])
        while queue:
            node, depth, visited = queue.popleft(); maximum = max(maximum, depth)
            if depth == 5: continue
            for target in adjacent[node]:
                if target not in visited: queue.append((target, depth + 1, visited | {target}))
    return maximum


def completion_priority(component_by_document: dict[int, int], repository: GraphRepository | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repository = repository or GraphRepository(); candidates = repository.all_candidates()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        kind, identifier = row["reference_kind"], row["normalized_document_number"]
        if row["resolution_status"] not in {"NOT_IN_CORPUS", "AMBIGUOUS", "NEEDS_REVIEW"} or not identifier:
            continue
        if kind in {"ACT", "RULE"} and not legal_identity_quality(identifier)[0]:
            continue
        groups[(kind, identifier)].append(row)
    ranked = []
    for (kind, identifier), rows in groups.items():
        source_ids = {int(row["source_document_id"]) for row in rows}
        components = {component_by_document[value] for value in source_ids}
        signals = sum(bool(row.get("candidate_relation_type")) for row in rows)
        high_quality = sum(not row.get("blocked_by_page_quality") for row in rows)
        dates = sorted({str(row.get("normalized_reference_date") or "") for row in rows} - {""})
        issuers = sorted({str(row.get("normalized_department") or "") for row in rows} - {""})
        local = any(row.get("resolved_target_catalog_id") for row in rows)
        adapter = kind in ADAPTER_KINDS
        complete = len(identifier) >= 8 and any(char.isdigit() for char in identifier)
        bridge = max(0, len(components) - 1)
        score = (
            len(source_ids) * 20 + len(rows) * 4 + bridge * 30 + signals * 15 + int(complete) * 8
            + int(bool(dates)) * 5 + int(bool(issuers)) * 4 + min(high_quality, 5) * 2
            + int(adapter) * 5 + int(local) * 25
        )
        ranked.append({
            "normalized_target_identity": f"{kind}|{identifier}",
            "raw_variants": ";".join(sorted({str(row["raw_document_number"]) for row in rows if row["raw_document_number"]})),
            "document_kind": kind, "date": ";".join(dates), "issuer": ";".join(issuers),
            "distinct_citing_documents": len(source_ids), "mentions": len(rows), "relation_signals": signals,
            "current_resolution_status": ";".join(sorted({row["resolution_status"] for row in rows})),
            "component_bridge_score": bridge, "citing_components": ";".join(map(str, sorted(components))),
            "citing_document_ids": ";".join(map(str, sorted(source_ids))),
            "source_quality": high_quality / len(rows), "official_source_adapter_availability": adapter,
            "locally_available": local, "priority_score": score,
            "priority_explanation": f"{len(source_ids)} citing docs; {len(rows)} mentions; {len(components)} citing components; {signals} strong signals; local={local}; adapter={adapter}",
        })
    ranked.sort(key=lambda row: (-row["priority_score"], row["normalized_target_identity"]))
    for rank, row in enumerate(ranked[:50], 1): row["rank"] = rank
    top = ranked[:50]
    bridges = [dict(row, priority=row["priority_score"]) for row in top if row["component_bridge_score"] > 0]
    return top, bridges


def performance(repository: GraphRepository | None = None) -> dict[str, Any]:
    repository = repository or GraphRepository(); service = GraphTraversalService(repository)
    with repository.connect() as connection:
        root = int(connection.execute("""SELECT source_document_id FROM kg_relations WHERE target_document_id IS NOT NULL
                                          GROUP BY source_document_id ORDER BY count(*) DESC LIMIT 1""").fetchone()["source_document_id"])
        target_row = connection.execute("SELECT target_document_id FROM kg_relations WHERE source_document_id=%s AND target_document_id IS NOT NULL LIMIT 1", (root,)).fetchone()
        target = int(target_row["target_document_id"]) if target_row else root
    def measure(callable_):
        started = perf_counter(); value = callable_(); return round((perf_counter() - started) * 1000, 3), value
    outgoing, _ = measure(lambda: service.upstream(root, 1)); incoming, _ = measure(lambda: service.downstream(root, 1))
    d2, _ = measure(lambda: service.mixed(root, 2)); d3, _ = measure(lambda: service.mixed(root, 3)); d5, _ = measure(lambda: service.mixed(root, 5))
    shortest, path = measure(lambda: service.paths(root, target, 5))
    components, _ = measure(lambda: topology(repository))
    return {"root_document_id": root, "target_document_id": target,
            "direct_outgoing_ms": outgoing, "direct_incoming_ms": incoming,
            "depth_2_ms": d2, "depth_3_ms": d3, "depth_5_ms": d5,
            "shortest_path_ms": shortest, "shortest_path_status": path["status"],
            "component_computation_ms": components, "indexes_added": [],
            "measured_reason_for_neo4j": False,
            "conclusion": "Current bounded PostgreSQL traversal latency provides no measured reason to introduce Neo4j."}


__all__ = ["STRONG_TYPES", "administrative_effects", "completion_priority", "performance", "relation_diagnostics", "topology"]
