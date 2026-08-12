from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from knowledge_engine.settings import settings


class EmbeddingProvider(Protocol):
    def encode(self, texts: list[str], show_progress_bar: bool = False): ...


class BgeM3Embedder:
    """Validated BAAI/bge-m3 embedding behavior, loaded only when instantiated."""

    def __init__(self, model_name: str = settings.embed_model):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.model = SentenceTransformer(model_name, cache_folder=str(settings.cache_dir / "models"))

    def encode(self, texts: list[str], show_progress_bar: bool = False):
        import numpy as np

        if not texts:
            return np.empty((0, settings.embed_dim), dtype=np.float32)
        vectors = self.model.encode(
            texts,
            batch_size=settings.embed_batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=show_progress_bar,
        )
        if vectors.shape[1] != settings.embed_dim:
            raise ValueError(
                f"Embedding dimension mismatch: model returned {vectors.shape[1]}, "
                f"but EMBED_DIM={settings.embed_dim}. Update configuration and SQL schema together."
            )
        return vectors.astype(np.float32)


Embedder = BgeM3Embedder


@lru_cache(maxsize=1)
def get_embedder() -> BgeM3Embedder:
    """Load BGE-M3 once per application process, on first actual search."""
    return BgeM3Embedder()
