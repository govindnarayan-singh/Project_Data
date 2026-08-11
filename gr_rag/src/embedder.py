from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from .config import settings


class Embedder:
    def __init__(self, model_name: str = settings.embed_model):
        self.model_name = model_name
        self.model = SentenceTransformer(
            model_name,
            cache_folder=str(settings.cache_dir / "models"),
        )

    def encode(self, texts: list[str], show_progress_bar: bool = False) -> np.ndarray:
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
                f"but EMBED_DIM={settings.embed_dim}. Update .env and SQL schema together."
            )
        return vectors.astype(np.float32)
