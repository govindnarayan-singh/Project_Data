from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol

from knowledge_engine.embeddings.bge_m3 import EmbeddingProvider, get_embedder
from knowledge_engine.retrieval.models import SearchResult
from knowledge_engine.retrieval.repository import PostgresRetrievalRepository, RetrievalRepository


ALLOWED_VALIDATION_STATES = frozenset({"AUTO_VERIFIED", "HUMAN_CONFIRMED"})


class GraphDirection(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BIDIRECTIONAL = "bidirectional"


@dataclass(frozen=True, slots=True)
class GraphPath:
    relation_id: int
    relation_type: str
    direction: str
    validation_state: str
    source_seed_document_id: int
    seed_dense_rank: int
    seed_dense_similarity: float
    source_document_id: int
    target_document_id: int
    source_evidence_page: int
    raw_edge_evidence: str
    candidate_id: int | None
    graph_distance: int = 1


@dataclass(frozen=True, slots=True)
class GraphDocument:
    document_id: int
    file_name: str
    relative_path: str
    department: str
    gr_number: str | None
    subject: str | None
    graph_added: bool
    duplicate_state: str
    dense_rank: int | None
    paths: tuple[GraphPath, ...]
    query_relevant_page_start: int | None = None
    query_relevant_page_end: int | None = None
    query_relevant_page_similarity: float | None = None
    query_relevant_page_text: str = ""


@dataclass(frozen=True, slots=True)
class GraphSearchResponse:
    query: str
    dense_top_k: int
    seed_top_k: int
    direction: str
    max_graph_additions_per_seed: int
    max_graph_additions_total: int
    dense_results: tuple[SearchResult, ...]
    graph_additions: tuple[GraphDocument, ...]
    corroborations: tuple[GraphDocument, ...]
    raw_neighbors: int
    unique_neighbors: int
    limits_reached: tuple[str, ...]
    timings_ms: dict[str, float]


class GraphExpansionRepository(Protocol):
    def validated_reference_neighbors(
        self, seed_document_ids: list[int], direction: GraphDirection
    ) -> list[dict[str, Any]]: ...

    def dense_search_document(
        self, query_vector: Any, document_id: int, top_k: int = 1
    ) -> list[dict[str, Any]]: ...


class PostgresGraphExpansionRepository(PostgresRetrievalRepository):
    """Read-only REFERENCES expansion and document-restricted page lookup."""

    def validated_reference_neighbors(
        self, seed_document_ids: list[int], direction: GraphDirection
    ) -> list[dict[str, Any]]:
        if not seed_document_ids:
            return []
        clauses: list[str] = []
        parameters: list[Any] = []
        if direction in {GraphDirection.OUTGOING, GraphDirection.BIDIRECTIONAL}:
            clauses.append("r.source_document_id = ANY(%s)")
            parameters.append(seed_document_ids)
        if direction in {GraphDirection.INCOMING, GraphDirection.BIDIRECTIONAL}:
            clauses.append("r.target_document_id = ANY(%s)")
            parameters.append(seed_document_ids)
        sql = f"""
            SELECT r.id AS relation_id,r.relation_type,r.validation_status,
                   r.source_document_id,r.target_document_id,
                   source.file_name AS source_file,target.file_name AS target_file,
                   evidence.candidate_id,evidence.source_page,evidence.raw_evidence_text,
                   target.file_path AS target_file_path,target.department AS target_department,
                   target.gr_number AS target_gr_number,target.subject AS target_subject,
                   source.file_path AS source_file_path,source.department AS source_department,
                   source.gr_number AS source_gr_number,source.subject AS source_subject
              FROM kg_relations r
              JOIN documents source ON source.id=r.source_document_id
              JOIN documents target ON target.id=r.target_document_id
              LEFT JOIN LATERAL (
                    SELECT c.id AS candidate_id,c.source_page,c.raw_evidence_text
                      FROM kg_relation_evidence re
                      JOIN kg_reference_candidates c ON c.id=re.candidate_id
                     WHERE re.relation_id=r.id
                     ORDER BY c.source_page,c.id LIMIT 1
              ) evidence ON true
             WHERE r.relation_type='REFERENCES'
               AND r.validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED')
               AND ({' OR '.join(clauses)})
             ORDER BY r.id
        """
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]

    def dense_search_document(
        self, query_vector: Any, document_id: int, top_k: int = 1
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.id AS chunk_id,d.id AS document_id,d.file_name,d.file_path,
                          d.department,d.gr_number,d.gr_date,d.subject,c.chunk_index,
                          c.page_start,c.page_end,c.text,1-(c.embedding <=> %s) AS similarity
                     FROM chunks c JOIN documents d ON d.id=c.document_id
                    WHERE d.id=%s ORDER BY c.embedding <=> %s,c.id LIMIT %s""",
                (query_vector, document_id, query_vector, top_k),
            ).fetchall()
        return [dict(row) for row in rows]


def _search_result(row: dict[str, Any], rank: int) -> SearchResult:
    return SearchResult(
        document_id=int(row["document_id"]), rank=rank,
        similarity=float(row["similarity"]), file_name=str(row["file_name"]),
        relative_path=str(row["file_path"]), department=str(row["department"]),
        gr_number=row.get("gr_number"), gr_date=row.get("gr_date"), subject=row.get("subject"),
        chunk_index=int(row["chunk_index"]), page_start=int(row["page_start"]),
        page_end=int(row["page_end"]), text=str(row["text"]),
    )


class GraphAwareRetrievalService:
    """Experimental one-hop evidence expansion after unchanged dense retrieval."""

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        dense_repository: RetrievalRepository | None = None,
        graph_repository: GraphExpansionRepository | None = None,
    ):
        self.embedder = embedder or get_embedder()
        default_repository = PostgresGraphExpansionRepository()
        self.dense_repository = dense_repository or default_repository
        self.graph_repository = graph_repository or default_repository

    def search(
        self,
        query: str,
        *,
        dense_top_k: int = 10,
        seed_top_k: int = 3,
        direction: GraphDirection | str = GraphDirection.BIDIRECTIONAL,
        max_graph_additions_per_seed: int = 10,
        max_graph_additions_total: int = 25,
        retrieve_graph_pages: bool = True,
    ) -> GraphSearchResponse:
        query = query.strip()
        if not query:
            raise ValueError("Query cannot be empty.")
        direction = GraphDirection(direction)
        if dense_top_k <= 0 or seed_top_k not in {1, 3, 5}:
            raise ValueError("dense_top_k must be positive and seed_top_k must be 1, 3, or 5")
        if max_graph_additions_per_seed <= 0 or max_graph_additions_total <= 0:
            raise ValueError("Graph expansion limits must be positive")

        total_start = perf_counter()
        started = perf_counter()
        vector = self.embedder.encode([query], show_progress_bar=False)[0]
        embedding_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        dense_rows = self.dense_repository.dense_search(vector, top_k=dense_top_k, department=None)
        dense_ms = (perf_counter() - started) * 1000
        dense_results = tuple(_search_result(row, rank) for rank, row in enumerate(dense_rows, 1))

        seed_rank: dict[int, tuple[int, float]] = {}
        for result in dense_results:
            if result.document_id not in seed_rank and len(seed_rank) < seed_top_k:
                seed_rank[result.document_id] = (result.rank, result.similarity)
        dense_doc_rank: dict[int, int] = {}
        for result in dense_results:
            dense_doc_rank.setdefault(result.document_id, result.rank)

        started = perf_counter()
        edge_rows = self.graph_repository.validated_reference_neighbors(list(seed_rank), direction)
        graph_sql_ms = (perf_counter() - started) * 1000
        candidates: list[tuple[int, int, dict[str, Any], str]] = []
        limits: set[str] = set()
        for row in edge_rows:
            source_id, target_id = int(row["source_document_id"]), int(row["target_document_id"])
            paths: list[tuple[int, int, str]] = []
            if source_id in seed_rank and direction in {GraphDirection.OUTGOING, GraphDirection.BIDIRECTIONAL}:
                paths.append((source_id, target_id, "OUTGOING"))
            if target_id in seed_rank and direction in {GraphDirection.INCOMING, GraphDirection.BIDIRECTIONAL}:
                paths.append((target_id, source_id, "INCOMING"))
            for seed_id, neighbor_id, edge_direction in paths:
                rank, _score = seed_rank[seed_id]
                candidates.append((rank, neighbor_id, row, edge_direction))
        candidates.sort(key=lambda item: (item[0], str(item[2]["relation_type"]), item[1], int(item[2]["relation_id"]), item[3]))

        selected: list[tuple[int, int, dict[str, Any], str]] = []
        selected_new_documents: set[int] = set()
        per_seed_documents: dict[int, set[int]] = {seed: set() for seed in seed_rank}
        for item in candidates:
            neighbor_id = item[1]
            seed_id = int(item[2]["source_document_id"] if item[3] == "OUTGOING" else item[2]["target_document_id"])
            if neighbor_id in dense_doc_rank:
                selected.append(item)
                continue
            if neighbor_id not in per_seed_documents[seed_id] and len(per_seed_documents[seed_id]) >= max_graph_additions_per_seed:
                limits.add(f"PER_SEED:{seed_id}")
                continue
            if neighbor_id not in selected_new_documents and len(selected_new_documents) >= max_graph_additions_total:
                limits.add("TOTAL")
                continue
            per_seed_documents[seed_id].add(neighbor_id)
            selected_new_documents.add(neighbor_id)
            selected.append(item)

        by_document: dict[int, list[GraphPath]] = {}
        row_by_document: dict[int, dict[str, Any]] = {}
        for _seed_rank, neighbor_id, row, edge_direction in selected:
            seed_id = int(row["source_document_id"] if edge_direction == "OUTGOING" else row["target_document_id"])
            dense_rank, dense_score = seed_rank[seed_id]
            path = GraphPath(
                relation_id=int(row["relation_id"]), relation_type=str(row["relation_type"]),
                direction=edge_direction, validation_state=str(row["validation_status"]),
                source_seed_document_id=seed_id, seed_dense_rank=dense_rank,
                seed_dense_similarity=dense_score, source_document_id=int(row["source_document_id"]),
                target_document_id=int(row["target_document_id"]),
                source_evidence_page=int(row.get("source_page") or 0),
                raw_edge_evidence=str(row.get("raw_evidence_text") or ""),
                candidate_id=int(row["candidate_id"]) if row.get("candidate_id") is not None else None,
            )
            by_document.setdefault(neighbor_id, []).append(path)
            row_by_document.setdefault(neighbor_id, row)

        additions: list[GraphDocument] = []
        corroborations: list[GraphDocument] = []
        page_ms = 0.0
        for document_id, paths in by_document.items():
            row = row_by_document[document_id]
            is_target = document_id == int(row["target_document_id"])
            prefix = "target" if is_target else "source"
            page_row: dict[str, Any] | None = None
            if retrieve_graph_pages and document_id not in dense_doc_rank:
                started = perf_counter()
                page_rows = self.graph_repository.dense_search_document(vector, document_id, top_k=1)
                page_ms += (perf_counter() - started) * 1000
                page_row = page_rows[0] if page_rows else None
            value = GraphDocument(
                document_id=document_id, file_name=str(row[f"{prefix}_file"]),
                relative_path=str(row[f"{prefix}_file_path"]), department=str(row[f"{prefix}_department"]),
                gr_number=row.get(f"{prefix}_gr_number"), subject=row.get(f"{prefix}_subject"),
                graph_added=document_id not in dense_doc_rank,
                duplicate_state="GRAPH_ADDED" if document_id not in dense_doc_rank else "ALREADY_IN_DENSE",
                dense_rank=dense_doc_rank.get(document_id), paths=tuple(paths),
                query_relevant_page_start=int(page_row["page_start"]) if page_row else None,
                query_relevant_page_end=int(page_row["page_end"]) if page_row else None,
                query_relevant_page_similarity=float(page_row["similarity"]) if page_row else None,
                query_relevant_page_text=str(page_row["text"]) if page_row else "",
            )
            (additions if value.graph_added else corroborations).append(value)

        timings = {
            "query_embedding": embedding_ms,
            "dense_sql": dense_ms,
            "graph_expansion_sql_service": graph_sql_ms,
            "restricted_graph_document_page_retrieval": page_ms,
            "total": (perf_counter() - total_start) * 1000,
        }
        return GraphSearchResponse(
            query=query, dense_top_k=dense_top_k, seed_top_k=seed_top_k,
            direction=direction.value, max_graph_additions_per_seed=max_graph_additions_per_seed,
            max_graph_additions_total=max_graph_additions_total, dense_results=dense_results,
            graph_additions=tuple(additions), corroborations=tuple(corroborations),
            raw_neighbors=len(candidates), unique_neighbors=len(by_document),
            limits_reached=tuple(sorted(limits)), timings_ms=timings,
        )


def response_as_dict(response: GraphSearchResponse) -> dict[str, Any]:
    return asdict(response)


__all__ = [
    "ALLOWED_VALIDATION_STATES", "GraphAwareRetrievalService", "GraphDirection",
    "GraphDocument", "GraphPath", "GraphSearchResponse", "PostgresGraphExpansionRepository",
    "response_as_dict",
]
