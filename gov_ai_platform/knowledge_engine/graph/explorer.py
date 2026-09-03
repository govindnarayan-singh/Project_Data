from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .repository import GraphRepository
from .types import RelationType, ValidationStatus


TRUSTED_VALIDATION_STATES = frozenset({
    ValidationStatus.AUTO_VERIFIED.value,
    ValidationStatus.HUMAN_CONFIRMED.value,
})
SUPPORTED_RELATION_TYPES = tuple(item.value for item in RelationType)
DEFAULT_MAX_NODES = 500
DEFAULT_MAX_EDGES = 2_000
HARD_MAX_NODES = 5_000
HARD_MAX_EDGES = 10_000
DOCUMENT_GRAPH_ROLES = (
    "ISOLATED_DOCUMENT",
    "REFERENCED_DOCUMENT",
    "CITING_DOCUMENT",
    "REFERENCED_AND_CITING_DOCUMENT",
)


def _document_graph_role(reference_degree: dict[str, int]) -> str:
    incoming = int(reference_degree.get("incoming") or 0)
    outgoing = int(reference_degree.get("outgoing") or 0)
    if incoming and outgoing:
        return "REFERENCED_AND_CITING_DOCUMENT"
    if incoming:
        return "REFERENCED_DOCUMENT"
    if outgoing:
        return "CITING_DOCUMENT"
    return "ISOLATED_DOCUMENT"


class ExplorerValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphFilters:
    relation_types: tuple[str, ...] = ()
    document_kinds: tuple[str, ...] = ()
    departments: tuple[str, ...] = ()
    validation_states: tuple[str, ...] = ()
    source_origins: tuple[str, ...] = ()
    year_min: int | None = None
    year_max: int | None = None
    connectivity: str = "all"
    relationship_presence: str = "any"
    component_id: str = ""
    graph_scope: str = "administrative"
    max_nodes: int = DEFAULT_MAX_NODES
    max_edges: int = DEFAULT_MAX_EDGES

    def normalized(self) -> "GraphFilters":
        relation_types = tuple(dict.fromkeys(value.upper() for value in self.relation_types if value))
        validation_states = tuple(dict.fromkeys(value.upper() for value in self.validation_states if value))
        unknown_relations = set(relation_types) - set(SUPPORTED_RELATION_TYPES)
        if unknown_relations:
            raise ExplorerValidationError(f"Unsupported relation type(s): {', '.join(sorted(unknown_relations))}")
        if set(validation_states) - TRUSTED_VALIDATION_STATES:
            raise ExplorerValidationError("Only trusted validation states may be visualized")
        if self.connectivity not in {"all", "connected", "isolated"}:
            raise ExplorerValidationError("connectivity must be all, connected, or isolated")
        if self.relationship_presence not in {"any", "incoming", "outgoing", "both"}:
            raise ExplorerValidationError("relationship_presence must be any, incoming, outgoing, or both")
        if self.graph_scope not in {"administrative", "documents"}:
            raise ExplorerValidationError("graph_scope must be administrative or documents")
        if self.year_min is not None and self.year_max is not None and self.year_min > self.year_max:
            raise ExplorerValidationError("year_min cannot be greater than year_max")
        return GraphFilters(
            relation_types=relation_types,
            document_kinds=tuple(dict.fromkeys(value for value in self.document_kinds if value)),
            departments=tuple(dict.fromkeys(value for value in self.departments if value)),
            validation_states=validation_states,
            source_origins=tuple(dict.fromkeys(value for value in self.source_origins if value)),
            year_min=self.year_min,
            year_max=self.year_max,
            connectivity=self.connectivity,
            relationship_presence=self.relationship_presence,
            component_id=self.component_id,
            graph_scope=self.graph_scope,
            max_nodes=min(max(1, int(self.max_nodes)), HARD_MAX_NODES),
            max_edges=min(max(1, int(self.max_edges)), HARD_MAX_EDGES),
        )


class GraphExplorerRepository:
    """Small read-only PostgreSQL projection for visualization and inspection."""

    def __init__(self, graph_repository: GraphRepository | None = None):
        self.graph_repository = graph_repository or GraphRepository()

    @contextmanager
    def _read_connection(self):
        with self.graph_repository.connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            yield connection

    def snapshot(self) -> dict[str, Any]:
        with self._read_connection() as connection:
            runtime = dict(connection.execute(
                """SELECT current_setting('server_version') AS postgresql_version,
                          current_database() AS database_name,
                          inet_server_addr()::text AS server_address,
                          inet_server_port() AS server_port,
                          (SELECT extversion FROM pg_extension WHERE extname='vector') AS pgvector_version"""
            ).fetchone())
            documents = [dict(row) for row in connection.execute(
                """SELECT id,file_name,file_path,sha256,department,document_type,gr_number,gr_date,
                          subject,page_count,digital_pages,ocr_pages,extraction_method,source_origin,
                          document_kind,canonical_identity,document_year,created_at
                     FROM documents ORDER BY id"""
            )]
            departments = [dict(row) for row in connection.execute(
                """SELECT d.id,d.canonical_name,d.normalized_name,d.created_at,d.updated_at,
                          coalesce(array_agg(a.raw_alias ORDER BY a.raw_alias)
                            FILTER (WHERE a.id IS NOT NULL),ARRAY[]::text[]) AS aliases
                     FROM kg_departments d
                     LEFT JOIN kg_department_aliases a ON a.department_id=d.id
                    GROUP BY d.id ORDER BY d.id"""
            )]
            relations = [dict(row) for row in connection.execute(
                """SELECT r.id,r.source_document_id,r.target_document_id,r.target_department_id,
                          r.relation_type,r.validation_status,r.created_from_candidate_id,
                          r.created_at,r.updated_at,
                          s.file_name AS source_file,s.gr_number AS source_number,
                          t.file_name AS target_file,t.gr_number AS target_number,
                          dep.canonical_name AS target_department,
                          count(DISTINCT e.id)::int AS evidence_count,
                          min(c.source_page) AS evidence_page,
                          min(c.raw_evidence_text) AS evidence_excerpt,
                          min(c.resolution_status) AS resolution_state,
                          min(c.parser_version) AS parser_version
                     FROM kg_relations r
                     JOIN documents s ON s.id=r.source_document_id
                     LEFT JOIN documents t ON t.id=r.target_document_id
                     LEFT JOIN kg_departments dep ON dep.id=r.target_department_id
                     LEFT JOIN kg_relation_evidence e ON e.relation_id=r.id
                     LEFT JOIN kg_reference_candidates c ON c.id=e.candidate_id
                    WHERE r.validation_status = ANY(%s)
                    GROUP BY r.id,s.id,t.id,dep.id
                    ORDER BY r.id""",
                (list(sorted(TRUSTED_VALIDATION_STATES)),),
            )]
        return {"runtime": runtime, "documents": documents, "departments": departments, "relations": relations}

    def document_details(self, document_id: int) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """SELECT d.id,d.file_name,d.file_path,d.sha256,d.department,d.document_type,d.gr_number,
                          d.gr_date,d.subject,d.page_count,d.digital_pages,d.ocr_pages,d.extraction_method,
                          d.source_origin,d.document_kind,d.canonical_identity,d.document_year,d.created_at,
                          count(DISTINCT c.id)::int AS chunk_count,
                          ep.external_candidate_id,ep.archive_source_root,ep.archive_relative_path,
                          ep.archived_sha256,ep.archived_at,
                          ec.source_authority,ec.source_adapter,ec.source_domain,ec.source_url,ec.final_url
                     FROM documents d
                     LEFT JOIN chunks c ON c.document_id=d.id
                     LEFT JOIN external_document_provenance ep ON ep.document_id=d.id
                     LEFT JOIN external_acquisition_candidates ec ON ec.id=ep.external_candidate_id
                    WHERE d.id=%s
                    GROUP BY d.id,ep.document_id,ec.id""",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def department_details(self, department_id: int) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """SELECT d.id,d.canonical_name,d.normalized_name,d.created_at,d.updated_at,
                          coalesce(array_agg(a.raw_alias ORDER BY a.raw_alias)
                            FILTER (WHERE a.id IS NOT NULL),ARRAY[]::text[]) AS aliases,
                          count(DISTINCT r.source_document_id)::int AS document_count
                     FROM kg_departments d
                     LEFT JOIN kg_department_aliases a ON a.department_id=d.id
                     LEFT JOIN kg_relations r ON r.target_department_id=d.id
                       AND r.relation_type='ISSUED_BY' AND r.validation_status=ANY(%s)
                    WHERE d.id=%s GROUP BY d.id""",
                (list(sorted(TRUSTED_VALIDATION_STATES)), department_id),
            ).fetchone()
        return dict(row) if row else None

    def edge_details(self, relation_id: int) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            relation = connection.execute(
                """SELECT r.id,r.relation_type,r.validation_status,r.source_document_id,
                          r.target_document_id,r.target_department_id,r.created_from_candidate_id,
                          r.created_at,r.updated_at,
                          s.file_name AS source_file,s.gr_number AS source_number,s.subject AS source_subject,
                          s.source_origin AS source_origin,s.file_path AS source_path,s.sha256 AS source_sha256,
                          t.file_name AS target_file,t.gr_number AS target_number,t.subject AS target_subject,
                          t.source_origin AS target_origin,t.file_path AS target_path,t.sha256 AS target_sha256,
                          dep.canonical_name AS target_department
                     FROM kg_relations r JOIN documents s ON s.id=r.source_document_id
                     LEFT JOIN documents t ON t.id=r.target_document_id
                     LEFT JOIN kg_departments dep ON dep.id=r.target_department_id
                    WHERE r.id=%s AND r.validation_status=ANY(%s)""",
                (relation_id, list(sorted(TRUSTED_VALIDATION_STATES))),
            ).fetchone()
            if relation is None:
                return None
            evidence = [dict(row) for row in connection.execute(
                """SELECT e.id AS relation_evidence_id,e.created_at AS evidence_link_created_at,
                          c.id AS candidate_id,c.source_document_id,c.source_page,c.reference_kind,
                          c.raw_evidence_text,c.raw_document_number,c.normalized_document_number,
                          c.raw_reference_date,c.normalized_reference_date,c.raw_department,
                          c.normalized_department,c.pattern_id,c.parser_version,c.parser_status,
                          c.parser_notes,c.resolution_status,c.resolution_method,c.resolution_evidence,
                          c.confidence_category,c.validation_status,c.evidence_hash,c.fingerprint,
                          c.created_at AS candidate_created_at,c.updated_at AS candidate_updated_at
                     FROM kg_relation_evidence e
                     JOIN kg_reference_candidates c ON c.id=e.candidate_id
                    WHERE e.relation_id=%s ORDER BY c.source_page,c.id""",
                (relation_id,),
            )]
        value = dict(relation)
        value["evidence"] = evidence
        value["provenance_complete"] = value["relation_type"] == "ISSUED_BY" or bool(evidence)
        return value

    def database_counts(self) -> dict[str, Any]:
        with self._read_connection() as connection:
            row = dict(connection.execute(
                """SELECT (SELECT count(*) FROM documents)::int AS documents,
                          (SELECT count(*) FROM pages)::int AS pages,
                          (SELECT count(*) FROM chunks)::int AS chunks,
                          (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL)::int AS embeddings,
                          (SELECT count(*) FROM kg_reference_candidates)::int AS kg_candidates,
                          (SELECT count(*) FROM kg_relations)::int AS kg_relations,
                          (SELECT count(*) FROM kg_relation_evidence)::int AS relation_evidence"""
            ).fetchone())
            counts = [dict(item) for item in connection.execute(
                "SELECT relation_type,count(*)::int AS count FROM kg_relations GROUP BY relation_type"
            )]
        row["relation_counts"] = {item["relation_type"]: item["count"] for item in counts}
        return row


def _document_year(document: dict[str, Any]) -> int | None:
    if document.get("document_year") is not None:
        return int(document["document_year"])
    value = document.get("gr_date")
    return int(value.year) if value else None


def _document_display_label(document: dict[str, Any]) -> str:
    """Return a deterministic human label without changing canonical identity."""
    number = str(document.get("gr_number") or "").strip()
    if number:
        return re.sub(r"\s+", " ", number)[:120]
    subject = str(document.get("subject") or "").strip()
    if subject:
        label = re.sub(r"[()]", "", subject)
        label = re.sub(r"\s+", " ", label).strip()
        if label.casefold().startswith("the "):
            label = label[4:]
        return label[:120]
    canonical = str(document.get("canonical_identity") or "").strip()
    if canonical:
        label = re.sub(r"[_:]+", " ", canonical)
        return re.sub(r"\s+", " ", label).strip().title()[:120]
    return str(document.get("file_name") or f"Document {document.get('id', '')}")[:120]


def _component_rows(document_ids: Iterable[int], relations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted(set(int(value) for value in document_ids))
    parent = {value: value for value in ids}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    document_edges = []
    for relation in relations:
        target = relation.get("target_document_id")
        source = relation.get("source_document_id")
        if source in parent and target in parent:
            union(int(source), int(target))
            document_edges.append(relation)
    members: dict[int, list[int]] = defaultdict(list)
    for value in ids:
        members[find(value)].append(value)
    edge_counts: Counter[int] = Counter()
    for relation in document_edges:
        edge_counts[find(int(relation["source_document_id"]))] += 1
    rows = [{
        "component_id": f"component:{min(values)}",
        "document_ids": values,
        "document_count": len(values),
        "node_count": len(values),
        "edge_count": edge_counts[root],
        "relation_count": edge_counts[root],
        "isolated": len(values) == 1 and edge_counts[root] == 0,
    } for root, values in members.items()]
    rows.sort(key=lambda row: (-row["document_count"], -row["edge_count"], row["document_ids"][0]))
    if rows:
        largest = rows[0]["document_count"]
        for row in rows:
            row["largest"] = row["document_count"] == largest
    return rows


class GraphExplorerService:
    def __init__(self, repository: GraphExplorerRepository | None = None):
        self.repository = repository or GraphExplorerRepository()

    @staticmethod
    def _validated_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        relations = [
            row for row in snapshot["relations"]
            if row.get("validation_status") in TRUSTED_VALIDATION_STATES
        ]
        return {**snapshot, "relations": relations}

    @staticmethod
    def _degrees(document_ids: Iterable[int], relations: Iterable[dict[str, Any]]) -> dict[int, dict[str, int]]:
        result = {int(value): {"incoming": 0, "outgoing": 0, "degree": 0} for value in document_ids}
        neighbors: dict[int, set[int]] = defaultdict(set)
        for relation in relations:
            target = relation.get("target_document_id")
            if target is None:
                continue
            source = int(relation["source_document_id"])
            target = int(target)
            if source in result:
                result[source]["outgoing"] += 1
                neighbors[source].add(target)
            if target in result:
                result[target]["incoming"] += 1
                neighbors[target].add(source)
        for document_id in result:
            result[document_id]["degree"] = len(neighbors[document_id])
        return result

    @staticmethod
    def _fingerprint(snapshot: dict[str, Any]) -> str:
        payload = {
            "documents": [(row["id"], row["sha256"]) for row in snapshot["documents"]],
            "departments": [(row["id"], row["normalized_name"]) for row in snapshot["departments"]],
            "relations": [(
                row["id"], row["source_document_id"], row.get("target_document_id"),
                row.get("target_department_id"), row["relation_type"], row["validation_status"],
            ) for row in snapshot["relations"]],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _state(self) -> dict[str, Any]:
        return self._validated_snapshot(self.repository.snapshot())

    def get_graph_summary(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._validated_snapshot(snapshot) if snapshot is not None else self._state()
        documents, departments, relations = state["documents"], state["departments"], state["relations"]
        document_ids = [int(row["id"]) for row in documents]
        document_edges = [row for row in relations if row.get("target_document_id") is not None]
        degrees = self._degrees(document_ids, document_edges)
        connected = sum(value["degree"] > 0 for value in degrees.values())
        components = _component_rows(document_ids, document_edges)
        relation_counts = Counter(row["relation_type"] for row in relations)
        for relation_type in SUPPORTED_RELATION_TYPES:
            relation_counts.setdefault(relation_type, 0)
        top_cited = sorted(documents, key=lambda row: (-degrees[int(row["id"])]["incoming"], int(row["id"])))[:5]
        top_citing = sorted(documents, key=lambda row: (-degrees[int(row["id"])]["outgoing"], int(row["id"])))[:5]
        most_connected = sorted(documents, key=lambda row: (-degrees[int(row["id"])]["degree"], int(row["id"])))[:5]
        department_counts = Counter(row.get("department") or "Unknown" for row in documents)
        active_department_ids = {
            int(row["target_department_id"]) for row in relations
            if row.get("target_department_id") is not None and row["relation_type"] == "ISSUED_BY"
        }
        references = relation_counts.get("REFERENCES", 0)
        issued_by = relation_counts.get("ISSUED_BY", 0)
        return {
            "total_documents": len(documents),
            "total_department_registry": len(departments),
            "active_departments": len(active_department_ids),
            "total_graph_nodes": len(documents) + len(active_department_ids),
            "references": references,
            "issued_by": issued_by,
            "document_relations": len(document_edges),
            "total_validated_edges": len(relations),
            "documents": len(documents),
            "departments": len(departments),
            "validated_relations": len(relations),
            "validated_document_relations": len(document_edges),
            "relation_counts": dict(sorted(relation_counts.items())),
            "connected_documents": connected,
            "isolated_documents": len(documents) - connected,
            "graph_coverage": connected / len(documents) if documents else 0.0,
            "components": len(components),
            "largest_component": components[0]["document_count"] if components else 0,
            "top_cited": [self._rank_item(row, degrees[int(row["id"])]["incoming"]) for row in top_cited],
            "top_citing": [self._rank_item(row, degrees[int(row["id"])]["outgoing"]) for row in top_citing],
            "most_connected": [self._rank_item(row, degrees[int(row["id"])]["degree"]) for row in most_connected],
            "department_document_counts": [{"department": key, "count": value} for key, value in department_counts.most_common()],
            "filter_options": {
                "relation_types": list(SUPPORTED_RELATION_TYPES),
                "document_kinds": sorted({str(row.get("document_kind") or row.get("document_type") or "UNKNOWN") for row in documents}),
                "departments": sorted({str(row.get("department") or "") for row in documents if row.get("department")}),
                "years": sorted({year for row in documents if (year := _document_year(row)) is not None}, reverse=True),
                "validation_states": sorted(TRUSTED_VALIDATION_STATES),
                "source_origins": sorted({str(row.get("source_origin") or "UNKNOWN") for row in documents}),
            },
            "runtime": state["runtime"],
            "graph_fingerprint": self._fingerprint(state),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
        }

    @staticmethod
    def _rank_item(document: dict[str, Any], count: int) -> dict[str, Any]:
        return {
            "document_id": int(document["id"]),
            "label": _document_display_label(document),
            "subject": document.get("subject") or "",
            "count": count,
        }

    def get_components(self) -> dict[str, Any]:
        state = self._state()
        rows = _component_rows(
            [int(row["id"]) for row in state["documents"]],
            [row for row in state["relations"] if row.get("target_document_id") is not None],
        )
        return {
            "items": rows,
            "count": len(rows),
            "largest_component": rows[0]["document_count"] if rows else 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "graph_fingerprint": self._fingerprint(state),
            "read_only": True,
        }

    def get_full_graph(self, filters: GraphFilters | None = None) -> dict[str, Any]:
        state = self._state()
        return self._build_graph(state, (filters or GraphFilters()).normalized(), mode="full")

    def get_document_neighborhood(
        self, document_id: int, *, direction: str = "both", depth: int = 1,
        filters: GraphFilters | None = None,
    ) -> dict[str, Any]:
        if direction not in {"incoming", "outgoing", "both"}:
            raise ExplorerValidationError("direction must be incoming, outgoing, or both")
        if depth not in {1, 2, 3}:
            raise ExplorerValidationError("depth must be 1, 2, or 3")
        state = self._state()
        all_ids = {int(row["id"]) for row in state["documents"]}
        if document_id not in all_ids:
            raise KeyError(document_id)
        graph_filters = (filters or GraphFilters()).normalized()
        allowed_types = set(graph_filters.relation_types or SUPPORTED_RELATION_TYPES) - {"ISSUED_BY"}
        edges = [
            row for row in state["relations"]
            if row.get("target_document_id") is not None and row["relation_type"] in allowed_types
            and (not graph_filters.validation_states or row["validation_status"] in graph_filters.validation_states)
        ]
        incoming: dict[int, list[int]] = defaultdict(list)
        outgoing: dict[int, list[int]] = defaultdict(list)
        for row in edges:
            source, target = int(row["source_document_id"]), int(row["target_document_id"])
            outgoing[source].append(target)
            incoming[target].append(source)
        selected = {document_id}
        queue = deque([(document_id, 0)])
        while queue:
            current, distance = queue.popleft()
            if distance >= depth:
                continue
            candidates: list[int] = []
            if direction in {"outgoing", "both"}:
                candidates.extend(outgoing[current])
            if direction in {"incoming", "both"}:
                candidates.extend(incoming[current])
            for candidate in sorted(set(candidates)):
                if candidate not in selected:
                    selected.add(candidate)
                    queue.append((candidate, distance + 1))
        graph_filters = GraphFilters(
            **{name: getattr(graph_filters, name) for name in GraphFilters.__dataclass_fields__ if name != "component_id"},
            component_id="",
        )
        return self._build_graph(
            state, graph_filters, mode="neighborhood", forced_document_ids=selected,
            mode_metadata={"root_document_id": document_id, "direction": direction, "depth": depth},
        )

    def _build_graph(
        self, state: dict[str, Any], filters: GraphFilters, *, mode: str,
        forced_document_ids: set[int] | None = None, mode_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        documents, departments, all_relations = state["documents"], state["departments"], state["relations"]
        document_edges = [row for row in all_relations if row.get("target_document_id") is not None]
        reference_edges = [row for row in document_edges if row["relation_type"] == "REFERENCES"]
        degrees = self._degrees([int(row["id"]) for row in documents], document_edges)
        reference_degrees = self._degrees([int(row["id"]) for row in documents], reference_edges)
        for value in reference_degrees.values():
            value["degree"] = value["incoming"] + value["outgoing"]
        component_rows = _component_rows([int(row["id"]) for row in documents], document_edges)
        component_members = {row["component_id"]: set(row["document_ids"]) for row in component_rows}
        allowed_component = component_members.get(filters.component_id) if filters.component_id else None
        if filters.component_id and allowed_component is None:
            raise ExplorerValidationError("Unknown component_id")

        def includes(document: dict[str, Any]) -> bool:
            document_id = int(document["id"])
            if forced_document_ids is not None and document_id not in forced_document_ids:
                return False
            if allowed_component is not None and document_id not in allowed_component:
                return False
            kind = str(document.get("document_kind") or document.get("document_type") or "UNKNOWN")
            if filters.document_kinds and kind not in filters.document_kinds:
                return False
            if filters.departments and str(document.get("department") or "") not in filters.departments:
                return False
            if filters.source_origins and str(document.get("source_origin") or "UNKNOWN") not in filters.source_origins:
                return False
            year = _document_year(document)
            if filters.year_min is not None and (year is None or year < filters.year_min):
                return False
            if filters.year_max is not None and (year is None or year > filters.year_max):
                return False
            degree = degrees[document_id]
            if filters.connectivity == "connected" and degree["degree"] == 0:
                return False
            if filters.connectivity == "isolated" and degree["degree"] != 0:
                return False
            if filters.relationship_presence == "incoming" and degree["incoming"] == 0:
                return False
            if filters.relationship_presence == "outgoing" and degree["outgoing"] == 0:
                return False
            if filters.relationship_presence == "both" and (degree["incoming"] == 0 or degree["outgoing"] == 0):
                return False
            return True

        matching_documents = [row for row in documents if includes(row)]
        total_matching_documents = len(matching_documents)
        selected_documents = matching_documents[:filters.max_nodes]
        selected_ids = {int(row["id"]) for row in selected_documents}
        relation_types = set(filters.relation_types or SUPPORTED_RELATION_TYPES)
        if filters.graph_scope == "documents":
            relation_types.discard("ISSUED_BY")
        validation_states = set(filters.validation_states or TRUSTED_VALIDATION_STATES)
        candidate_relations = [
            row for row in all_relations
            if row["relation_type"] in relation_types and row["validation_status"] in validation_states
            and int(row["source_document_id"]) in selected_ids
            and (row.get("target_document_id") is None or int(row["target_document_id"]) in selected_ids)
        ]
        department_ids = sorted({
            int(row["target_department_id"]) for row in candidate_relations
            if row.get("target_department_id") is not None
        })
        room = max(0, filters.max_nodes - len(selected_documents))
        included_department_ids = set(department_ids[:room])
        selected_departments = [row for row in departments if int(row["id"]) in included_department_ids]
        candidate_relations = [
            row for row in candidate_relations
            if row.get("target_department_id") is None or int(row["target_department_id"]) in included_department_ids
        ]
        total_matching_edges = len(candidate_relations)
        selected_relations = candidate_relations[:filters.max_edges]
        nodes = [
            self._document_node(row, degrees[int(row["id"])], reference_degrees[int(row["id"])])
            for row in selected_documents
        ]
        nodes.extend(self._department_node(row, all_relations) for row in selected_departments)
        edges = [self._edge(row) for row in selected_relations]
        node_role_counts = Counter(node.get("graph_role") or node["type"] for node in nodes)
        displayed_relation_counts = Counter(row["relation_type"] for row in selected_relations)
        summary = self.get_graph_summary(state)
        reasons = []
        if total_matching_documents > len(selected_documents):
            reasons.append(f"document limit {filters.max_nodes} reached")
        if len(department_ids) > len(included_department_ids):
            reasons.append(f"node limit {filters.max_nodes} prevented some department nodes")
        if total_matching_edges > len(selected_relations):
            reasons.append(f"edge limit {filters.max_edges} reached")
        return {
            "nodes": nodes,
            "edges": edges,
            "summary": summary,
            "counts": {
                "total_documents": summary["total_documents"],
                "total_department_registry": summary["total_department_registry"],
                "active_departments": summary["active_departments"],
                "total_graph_nodes": summary["total_graph_nodes"],
                "references": summary["references"],
                "issued_by": summary["issued_by"],
                "document_relations": summary["document_relations"],
                "total_validated_edges": summary["total_validated_edges"],
                "displayed_documents": len(selected_documents),
                "displayed_departments": len(selected_departments),
                "displayed_other_nodes": 0,
                "displayed_nodes": len(nodes),
                "displayed_references": displayed_relation_counts.get("REFERENCES", 0),
                "displayed_issued_by": displayed_relation_counts.get("ISSUED_BY", 0),
                "displayed_document_relations": sum(
                    1 for row in selected_relations if row.get("target_document_id") is not None
                ),
                "displayed_edges": len(edges),
            },
            "filters": {
                "relation_types": list(filters.relation_types), "document_kinds": list(filters.document_kinds),
                "departments": list(filters.departments), "validation_states": list(filters.validation_states),
                "source_origins": list(filters.source_origins), "year_min": filters.year_min,
                "year_max": filters.year_max, "connectivity": filters.connectivity,
                "relationship_presence": filters.relationship_presence, "component_id": filters.component_id,
                "graph_scope": filters.graph_scope,
                "max_nodes": filters.max_nodes, "max_edges": filters.max_edges,
            },
            "mode": mode,
            "mode_metadata": mode_metadata or {},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "graph_fingerprint": self._fingerprint(state),
            "truncated": bool(reasons),
            "truncation_reason": "; ".join(reasons),
            "available": {"documents": total_matching_documents, "edges": total_matching_edges},
            "returned": {"nodes": len(nodes), "documents": len(selected_documents), "departments": len(selected_departments), "edges": len(edges)},
            "candidate_edges_included": False,
            "node_role_counts": {
                **{role: node_role_counts.get(role, 0) for role in DOCUMENT_GRAPH_ROLES},
                "DEPARTMENT": node_role_counts.get("DEPARTMENT", 0),
                "CANDIDATE_OR_UNRESOLVED": 0,
            },
            "read_only": True,
        }

    @staticmethod
    def _document_node(
        document: dict[str, Any], degree: dict[str, int], reference_degree: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        label = _document_display_label(document)
        reference_degree = reference_degree or degree
        return {
            "id": f"document:{document['id']}", "type": "DOCUMENT", "label": str(label)[:120],
            "graph_role": _document_graph_role(reference_degree),
            "reference_degree": dict(reference_degree),
            "metadata": {
                "document_id": int(document["id"]), "file_name": document["file_name"],
                "file_path": document["file_path"], "sha256": document["sha256"],
                "document_kind": document.get("document_kind") or document.get("document_type") or "UNKNOWN",
                "document_number": document.get("gr_number") or document.get("canonical_identity") or "",
                "canonical_identity": document.get("canonical_identity") or "",
                "display_label": label,
                "subject": document.get("subject") or "", "date": document.get("gr_date"),
                "year": _document_year(document), "department": document.get("department") or "",
                "source_origin": document.get("source_origin") or "UNKNOWN", "page_count": document.get("page_count") or 0,
                "pdf_url": f"/documents/{document['id']}/pdf/", "detail_url": f"/documents/{document['id']}/",
            },
            "degree": degree,
        }

    @staticmethod
    def _department_node(department: dict[str, Any], relations: list[dict[str, Any]]) -> dict[str, Any]:
        count = sum(
            row.get("target_department_id") == department["id"] and row["relation_type"] == "ISSUED_BY"
            for row in relations
        )
        return {
            "id": f"department:{department['id']}", "type": "DEPARTMENT",
            "label": department["canonical_name"],
            "metadata": {"department_id": int(department["id"]), "canonical_name": department["canonical_name"],
                         "aliases": list(department.get("aliases") or []), "document_count": count},
            "degree": {"incoming": count, "outgoing": 0, "degree": count},
        }

    @staticmethod
    def _edge(relation: dict[str, Any]) -> dict[str, Any]:
        target = (
            f"document:{relation['target_document_id']}" if relation.get("target_document_id") is not None
            else f"department:{relation['target_department_id']}"
        )
        return {
            "id": f"relation:{relation['id']}", "relation_id": int(relation["id"]),
            "source": f"document:{relation['source_document_id']}", "target": target,
            "relation_type": relation["relation_type"], "validation_state": relation["validation_status"],
            "provenance_summary": {
                "evidence_count": int(relation.get("evidence_count") or 0),
                "source_page": relation.get("evidence_page"),
                "evidence_excerpt": (relation.get("evidence_excerpt") or "")[:240],
                "candidate_id": relation.get("created_from_candidate_id"),
                "resolution_state": relation.get("resolution_state"),
                "parser_version": relation.get("parser_version"),
            },
        }

    def search_graph_nodes(self, query: str, *, filters: GraphFilters | None = None, limit: int = 25) -> dict[str, Any]:
        query = query.strip().casefold()
        if not query:
            return {"items": [], "count": 0, "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True}
        graph = self.get_full_graph(filters or GraphFilters(max_nodes=HARD_MAX_NODES, max_edges=HARD_MAX_EDGES))
        scored = []
        for node in graph["nodes"]:
            if node["type"] != "DOCUMENT":
                continue
            metadata = node["metadata"]
            fields = [str(metadata.get(key) or "") for key in (
                "document_id", "document_number", "subject", "year", "date", "department", "document_kind", "file_name",
            )]
            matches = [index for index, value in enumerate(fields) if query in value.casefold()]
            if matches:
                scored.append((min(matches), str(node["label"]).casefold(), node))
        items = [row[2] for row in sorted(scored, key=lambda value: (value[0], value[1], value[2]["id"]))[:min(max(1, limit), 100)]]
        return {"items": items, "count": len(items), "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True}

    def get_node_details(self, node_id: str) -> dict[str, Any] | None:
        try:
            node_type, raw_id = node_id.split(":", 1)
            numeric_id = int(raw_id)
        except (ValueError, TypeError):
            raise ExplorerValidationError("node_id must be document:<id> or department:<id>")
        state = self._state()
        if node_type == "document":
            document = self.repository.document_details(numeric_id)
            if document is None:
                return None
            relations = [
                row for row in state["relations"]
                if int(row["source_document_id"]) == numeric_id or row.get("target_document_id") == numeric_id
            ]
            incoming = [row for row in relations if row.get("target_document_id") == numeric_id]
            outgoing = [row for row in relations if int(row["source_document_id"]) == numeric_id]
            incoming_references = [row for row in incoming if row["relation_type"] == "REFERENCES"]
            outgoing_references = [row for row in outgoing if row["relation_type"] == "REFERENCES"]
            reference_degree = {
                "incoming": len(incoming_references),
                "outgoing": len(outgoing_references),
                "degree": len(incoming_references) + len(outgoing_references),
            }
            neighboring_ids = sorted({
                int(value) for row in relations
                for value in (row.get("source_document_id"), row.get("target_document_id"))
                if value is not None and int(value) != numeric_id
            })
            documents = {int(row["id"]): row for row in state["documents"]}
            return {
                "id": node_id, "type": "DOCUMENT", "document": document,
                "graph_role": _document_graph_role(reference_degree),
                "reference_degree": reference_degree,
                "incoming_relations": [self._edge(row) for row in incoming],
                "outgoing_relations": [self._edge(row) for row in outgoing],
                "relation_counts": dict(Counter(row["relation_type"] for row in relations)),
                "neighboring_documents": [self._document_node(documents[value], self._degrees(documents, state["relations"])[value]) for value in neighboring_ids],
                "connected_departments": sorted({str(row.get("target_department") or "") for row in outgoing if row["relation_type"] == "ISSUED_BY"}),
                "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            }
        if node_type == "department":
            department = self.repository.department_details(numeric_id)
            if department is None:
                return None
            issued = [row for row in state["relations"] if row.get("target_department_id") == numeric_id]
            return {
                "id": node_id, "type": "DEPARTMENT", "department": department,
                "incoming_relations": [self._edge(row) for row in issued], "outgoing_relations": [],
                "document_ids": sorted(int(row["source_document_id"]) for row in issued),
                "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            }
        raise ExplorerValidationError("node_id must be document:<id> or department:<id>")

    def get_edge_details(self, relation_id: int) -> dict[str, Any] | None:
        if not isinstance(relation_id, int) or relation_id <= 0:
            raise ExplorerValidationError("relation_id must be a positive integer")
        value = self.repository.edge_details(relation_id)
        if value is None:
            return None
        value["direction"] = "source_to_target"
        value["generated_at"] = datetime.now(timezone.utc).isoformat()
        value["read_only"] = True
        return value

    def get_topology_summary(self) -> dict[str, Any]:
        summary = self.get_graph_summary()
        return {key: summary[key] for key in (
            "documents", "validated_document_relations", "connected_documents", "isolated_documents",
            "graph_coverage", "components", "largest_component", "top_cited", "top_citing",
            "most_connected", "department_document_counts", "graph_fingerprint", "generated_at", "read_only",
        )}


__all__ = [
    "DEFAULT_MAX_EDGES", "DEFAULT_MAX_NODES", "ExplorerValidationError", "GraphExplorerRepository",
    "GraphExplorerService", "GraphFilters", "HARD_MAX_EDGES", "HARD_MAX_NODES",
    "SUPPORTED_RELATION_TYPES", "TRUSTED_VALIDATION_STATES",
]
