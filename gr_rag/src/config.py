from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _resolve_path(value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


@dataclass(frozen=True)
class Settings:
    gr_root: Path = _resolve_path(os.getenv("GR_ROOT", "./GRs"))
    cache_dir: Path = _resolve_path(os.getenv("CACHE_DIR", "./.cache_local"))
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://gr_user:gr_password@localhost:5432/grdb"
    )
    embed_model: str = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    embed_dim: int = int(os.getenv("EMBED_DIM", "1024"))
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "tesseract")
    ocr_lang: str = os.getenv("OCR_LANG", "mar+eng")
    ocr_dpi: int = int(os.getenv("OCR_DPI", "300"))
    chunk_max_tokens: int = int(os.getenv("CHUNK_MAX_TOKENS", "700"))
    chunk_overlap_tokens: int = int(os.getenv("CHUNK_OVERLAP_TOKENS", "100"))
    embed_batch_size: int = int(os.getenv("EMBED_BATCH_SIZE", "8"))


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)

# Keep every Hugging Face component in one cache.  An explicit CACHE_DIR is an
# application setting, so it must take precedence over inherited user-level
# Hugging Face variables; otherwise the tokenizer and model can be downloaded
# into different locations on Windows.
_huggingface_cache = settings.cache_dir / "models"
_huggingface_cache.mkdir(parents=True, exist_ok=True)
os.environ["HF_HOME"] = str(_huggingface_cache)
