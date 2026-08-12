from __future__ import annotations

from knowledge_engine.embeddings.bge_m3 import EmbeddingProvider
from knowledge_engine.retrieval.repository import RetrievalRepository


class DenseRetriever:
    """Small composition boundary preserving the validated cosine search."""

    def __init__(self, embedder: EmbeddingProvider, repository: RetrievalRepository):
        self.embedder = embedder
        self.repository = repository

    def search_rows(self, query: str, top_k: int, department: str | None = None):
        query_vector = self.embedder.encode([query], show_progress_bar=False)[0]
        return self.repository.dense_search(query_vector, top_k=top_k, department=department)
