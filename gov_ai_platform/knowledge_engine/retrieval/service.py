from __future__ import annotations

from collections.abc import Callable

from knowledge_engine.embeddings.bge_m3 import EmbeddingProvider, get_embedder
from knowledge_engine.retrieval.dense import DenseRetriever
from knowledge_engine.retrieval.models import SearchResult
from knowledge_engine.retrieval.repository import PostgresRetrievalRepository, RetrievalRepository


ALLOWED_TOP_K = frozenset({3, 5, 10})


class RetrievalService:
    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        repository: RetrievalRepository | None = None,
        embedder_factory: Callable[[], EmbeddingProvider] = get_embedder,
    ):
        self._embedder = embedder
        self._embedder_factory = embedder_factory
        self.repository = repository or PostgresRetrievalRepository()

    @property
    def embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    def search(self, query: str, top_k: int = 5, department: str | None = None) -> list[SearchResult]:
        query = query.strip()
        if not query:
            raise ValueError("Query cannot be empty.")
        if top_k not in ALLOWED_TOP_K:
            raise ValueError(f"top_k must be one of {sorted(ALLOWED_TOP_K)}.")
        department = department.strip() if department else None
        rows = DenseRetriever(self.embedder, self.repository).search_rows(query, top_k, department)
        return [
            SearchResult(
                document_id=int(row["document_id"]),
                rank=rank,
                similarity=float(row["similarity"]),
                file_name=row["file_name"],
                relative_path=row["file_path"],
                department=row["department"],
                gr_number=row["gr_number"],
                gr_date=row["gr_date"],
                subject=row["subject"],
                chunk_index=int(row["chunk_index"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=row["text"],
            )
            for rank, row in enumerate(rows, start=1)
        ]
