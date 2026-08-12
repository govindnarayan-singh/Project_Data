from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SearchResult:
    document_id: int
    rank: int
    similarity: float
    file_name: str
    relative_path: str
    department: str
    gr_number: str | None
    gr_date: date | None
    subject: str | None
    chunk_index: int
    page_start: int
    page_end: int
    text: str
