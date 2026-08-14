from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    gr_root: Path = _resolve_path(os.getenv("GR_ROOT", "../GRs"))
    cache_dir: Path = _resolve_path(os.getenv("CACHE_DIR", "../.cache_local"))
    database_url: str = os.getenv("DATABASE_URL", "postgresql://gr_user:gr_password@localhost:5433/grdb")
    embed_model: str = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    embed_dim: int = int(os.getenv("EMBED_DIM", "1024"))
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "tesseract")
    ocr_lang: str = os.getenv("OCR_LANG", "mar+eng")
    ocr_dpi: int = int(os.getenv("OCR_DPI", "300"))
    chunk_max_tokens: int = int(os.getenv("CHUNK_MAX_TOKENS", "700"))
    chunk_overlap_tokens: int = int(os.getenv("CHUNK_OVERLAP_TOKENS", "100"))
    embed_batch_size: int = int(os.getenv("EMBED_BATCH_SIZE", "8"))
    kg_review_mode: str = os.getenv("KG_REVIEW_MODE", "auto")
    kg_auto_ingest_resolved: bool = os.getenv("KG_AUTO_INGEST_RESOLVED", "false").strip().casefold() in {"1", "true", "yes", "on"}


settings = Settings()
# Match the validated prototype: every Hugging Face component uses the existing
# workspace cache even if the launching shell has a different user-level value.
os.environ["HF_HOME"] = str(settings.cache_dir / "models")
