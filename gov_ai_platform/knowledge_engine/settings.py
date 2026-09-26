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
    database_url: str = os.getenv("DATABASE_URL", "postgresql://gr_user:gr_password@127.0.0.1:5434/grdb")
    embed_model: str = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    embed_dim: int = int(os.getenv("EMBED_DIM", "1024"))
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "tesseract")
    ocr_lang: str = os.getenv("OCR_LANG", "mar+eng")
    ocr_dpi: int = int(os.getenv("OCR_DPI", "300"))
    chunk_max_tokens: int = int(os.getenv("CHUNK_MAX_TOKENS", "700"))
    chunk_overlap_tokens: int = int(os.getenv("CHUNK_OVERLAP_TOKENS", "100"))
    embed_batch_size: int = int(os.getenv("EMBED_BATCH_SIZE", "8"))
    kg_review_mode: str = os.getenv("KG_REVIEW_MODE", "auto")
    retrieval_mode: str = os.getenv("RETRIEVAL_MODE", "dense").strip().casefold()
    kg_auto_ingest_resolved: bool = os.getenv("KG_AUTO_INGEST_RESOLVED", "false").strip().casefold() in {"1", "true", "yes", "on"}
    # Compatibility name retained for existing catalogue/provenance code. New
    # acquisition writes land in a durable local Incoming directory first.
    external_acquisition_root: Path = _resolve_path(
        os.getenv(
            "EXTERNAL_INCOMING_ROOT",
            os.getenv("EXTERNAL_ACQUISITION_ROOT", "../Government_Documents/Incoming"),
        )
    )
    external_document_root: Path = _resolve_path(
        os.getenv("EXTERNAL_DOCUMENT_ROOT", "../Government_Documents/External_Official")
    )
    approved_submission_root: Path = _resolve_path(
        os.getenv("APPROVED_SUBMISSION_ROOT", "../Government_Documents/Approved_Submissions")
    )
    backup_root: Path = _resolve_path(
        os.getenv("BACKUP_ROOT", "../Backups/gov_ai_platform")
    )
    backup_keep_last: int = int(os.getenv("BACKUP_KEEP_LAST", "0"))
    postgres_backup_mode: str = os.getenv("POSTGRES_BACKUP_MODE", "native").strip().casefold()
    postgres_bin: Path = _resolve_path(
        os.getenv("POSTGRES_BIN", "../PostgreSQL/17/pgsql/bin")
    )
    postgres_data: Path = _resolve_path(
        os.getenv("POSTGRES_DATA", "../PostgreSQL/data17")
    )
    postgres_logs: Path = _resolve_path(
        os.getenv("POSTGRES_LOGS", "../PostgreSQL/logs")
    )
    postgres_container: str = os.getenv("POSTGRES_CONTAINER", "gr-postgres")
    external_http_timeout_seconds: float = float(os.getenv("EXTERNAL_HTTP_TIMEOUT_SECONDS", "15"))
    external_http_max_bytes: int = int(os.getenv("EXTERNAL_HTTP_MAX_BYTES", str(25 * 1024 * 1024)))
    external_http_max_redirects: int = int(os.getenv("EXTERNAL_HTTP_MAX_REDIRECTS", "3"))
    external_http_retry_count: int = int(os.getenv("EXTERNAL_HTTP_RETRY_COUNT", "1"))
    external_http_rate_delay_seconds: float = float(os.getenv("EXTERNAL_HTTP_RATE_DELAY_SECONDS", "0.5"))
    external_http_user_agent: str = os.getenv(
        "EXTERNAL_HTTP_USER_AGENT", "GovAIAdministrativeAcquisition/1.0 (+local research)"
    )

    @property
    def external_incoming_root(self) -> Path:
        return self.external_acquisition_root


settings = Settings()
# Match the validated prototype: every Hugging Face component uses the existing
# workspace cache even if the launching shell has a different user-level value.
os.environ["HF_HOME"] = str(settings.cache_dir / "models")
