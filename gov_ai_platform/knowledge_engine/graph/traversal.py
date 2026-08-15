from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .repository import GraphRepository


VALID_STATES = ("AUTO_VERIFIED", "HUMAN_CONFIRMED")
DOCUMENT_RELATIONS = (
    "REFERENCES", "AMENDS", "CORRECTS", "SUPERSEDES",
    "PARTIALLY_SUPERSEDES", "CANCELS", "EXTENDS",
)


def _depth(value: int | None) -> int:
    value = 3 if value is None else int(value)
    if not 1 <= value <= 5:
        raise ValueError("max_depth must be between 1 and 5")
    return value


@dataclass(frozen=True, slots=True)
class TraversalResult:
    status: str
    start_document_id: int
    direction: str
    max_depth: int
    paths: list[dict[str, Any]]
    cycles_detected: int


class GraphTraversalService:
    """Bounded, read-only traversal over validated PostgreSQL document edges."""

    def __init__(self, repository: GraphRepository | None = None):
        self.repository = repository or GraphRepository()

    @staticmethod
    def _types(relation_types: Iterable[str] | None) -> list[str]:
        values = [str(value).upper() for value in (relation_types or DOCUMENT_RELATIONS)]
        invalid = sorted(set(values) - set(DOCUMENT_RELATIONS))
        if invalid:
            raise ValueError(f"Unsupported document relation type(s): {', '.join(invalid)}")
        return values

    def _walk(self, document_id: int, max_depth: int, *, direction: str,
              relation_types: Iterable[str] | None = None) -> TraversalResult:
        depth = _depth(max_depth)
        types = self._types(relation_types)
        if direction not in {"upstream", "downstream", "mixed"}:
            raise ValueError("direction must be upstream, downstream, or mixed")
        if direction == "upstream":
            neighbor = "r.target_document_id"
            predicate = "r.source_document_id = w.current_id"
        elif direction == "downstream":
            neighbor = "r.source_document_id"
            predicate = "r.target_document_id = w.current_id"
        else:
            neighbor = "CASE WHEN r.source_document_id=w.current_id THEN r.target_document_id ELSE r.source_document_id END"
            predicate = "(r.source_document_id=w.current_id OR r.target_document_id=w.current_id)"
        query = f"""
            WITH RECURSIVE walk AS (
              SELECT %s::bigint start_id,%s::bigint current_id,0 depth,
                     ARRAY[%s::bigint] visited,ARRAY[]::bigint[] relation_ids,
                     false cycle
              UNION ALL
              SELECT w.start_id,{neighbor},w.depth+1,
                     w.visited || {neighbor},w.relation_ids || r.id,
                     {neighbor}=ANY(w.visited)
                FROM walk w JOIN kg_relations r ON {predicate}
               WHERE w.depth < %s AND r.target_document_id IS NOT NULL
                 AND r.validation_status=ANY(%s) AND r.relation_type=ANY(%s)
                 AND NOT w.cycle
            )
            SELECT current_id,depth,visited,relation_ids,cycle
              FROM walk WHERE depth>0 ORDER BY depth,relation_ids
        """
        with self.repository.connect() as connection:
            if not connection.execute("SELECT 1 FROM documents WHERE id=%s", (document_id,)).fetchone():
                raise ValueError(f"Document {document_id} does not exist")
            rows = [dict(row) for row in connection.execute(
                query, (document_id, document_id, document_id, depth, list(VALID_STATES), types)
            )]
            paths = [self._hydrate(connection, row) for row in rows]
        return TraversalResult(
            status="PASS", start_document_id=int(document_id), direction=direction,
            max_depth=depth, paths=paths,
            cycles_detected=sum(bool(row["cycle"]) for row in rows),
        )

    @staticmethod
    def _hydrate(connection, row: dict[str, Any]) -> dict[str, Any]:
        relation_ids = [int(value) for value in row["relation_ids"]]
        if not relation_ids:
            return {**row, "hops": []}
        relations = {
            int(item["relation_id"]): dict(item)
            for item in connection.execute(
                """SELECT r.id relation_id,r.source_document_id,r.target_document_id,r.relation_type,
                          r.validation_status,r.created_from_candidate_id,
                          s.file_name source_file,t.file_name target_file
                     FROM kg_relations r JOIN documents s ON s.id=r.source_document_id
                     JOIN documents t ON t.id=r.target_document_id
                    WHERE r.id=ANY(%s)""", (relation_ids,)
            )
        }
        evidence: dict[int, list[dict[str, Any]]] = {value: [] for value in relation_ids}
        for item in connection.execute(
            """SELECT e.relation_id,c.id candidate_id,c.source_page,c.raw_evidence_text,
                      c.evidence_hash,c.validation_status candidate_validation_status
                 FROM kg_relation_evidence e JOIN kg_reference_candidates c ON c.id=e.candidate_id
                WHERE e.relation_id=ANY(%s) ORDER BY e.relation_id,c.source_page,c.id""", (relation_ids,)
        ):
            evidence[int(item["relation_id"])].append(dict(item))
        hops = []
        for position, relation_id in enumerate(relation_ids, 1):
            relation = relations[relation_id]
            relation["evidence"] = evidence[relation_id]
            relation["depth"] = position
            relation["path_position"] = position
            relation["provenance_complete"] = bool(
                evidence[relation_id]
                and all(item["candidate_id"] and item["source_page"] and item["raw_evidence_text"] for item in evidence[relation_id])
            )
            hops.append(relation)
        return {
            "end_document_id": int(row["current_id"]), "depth": int(row["depth"]),
            "document_ids": [int(value) for value in row["visited"]],
            "relation_ids": relation_ids, "cycle": bool(row["cycle"]), "hops": hops,
        }

    def upstream(self, document_id: int, max_depth: int = 3,
                 relation_types: Iterable[str] | None = None) -> TraversalResult:
        return self._walk(document_id, max_depth, direction="upstream", relation_types=relation_types)

    def downstream(self, document_id: int, max_depth: int = 3,
                   relation_types: Iterable[str] | None = None) -> TraversalResult:
        return self._walk(document_id, max_depth, direction="downstream", relation_types=relation_types)

    def mixed(self, document_id: int, max_depth: int = 3,
              relation_types: Iterable[str] | None = None) -> TraversalResult:
        return self._walk(document_id, max_depth, direction="mixed", relation_types=relation_types)

    def paths(self, source_id: int, target_id: int, max_depth: int = 3,
              relation_types: Iterable[str] | None = None) -> dict[str, Any]:
        depth = _depth(max_depth)
        if source_id == target_id:
            return {"status": "PATH_FOUND", "path_length": 0, "document_ids": [source_id], "hops": [], "max_depth": depth}
        result = self.upstream(source_id, depth, relation_types)
        matches = [row for row in result.paths if row["end_document_id"] == int(target_id) and not row["cycle"]]
        if not matches:
            return {
                "status": "NO_PATH", "source_document_id": source_id, "target_document_id": target_id,
                "max_depth": depth,
                "note": "No directed validated path was found within the stated bound; this is not a claim beyond that bound.",
            }
        found = min(matches, key=lambda row: (row["depth"], row["relation_ids"]))
        return {"status": "PATH_FOUND", "source_document_id": source_id, "target_document_id": target_id,
                "max_depth": depth, "path_length": found["depth"], **found}

    def history(self, document_id: int, max_depth: int = 3) -> dict[str, Any]:
        result = self.mixed(document_id, max_depth)
        document_ids = sorted({document_id, *(item for row in result.paths for item in row["document_ids"])})
        with self.repository.connect() as connection:
            documents = [dict(row) for row in connection.execute(
                """SELECT id,file_name,document_kind,gr_number,gr_date,subject,department
                     FROM documents WHERE id=ANY(%s) ORDER BY gr_date NULLS LAST,id""", (document_ids,)
            )]
        return {
            "status": "PASS", "document_id": document_id, "max_depth": _depth(max_depth),
            "documents_chronological_when_available": documents, "paths": result.paths,
            "cycles_detected": result.cycles_detected,
            "semantics": "Chronology is descriptive only; no causality or legal operability is inferred.",
        }


__all__ = ["DOCUMENT_RELATIONS", "GraphTraversalService", "TraversalResult"]
