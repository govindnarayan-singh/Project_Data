from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from knowledge_engine.settings import PROJECT_ROOT


DEFAULT_OVERRIDES = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_quality_overrides.csv"


@dataclass(frozen=True, slots=True)
class QualityOverride:
    document_id: int
    file_name: str
    page_number: int | None
    field: str
    raw_value: str
    review_status: str
    reviewed_value: str
    reason: str
    kg_usage_policy: str


class QualityPolicy:
    def __init__(self, overrides: list[QualityOverride]):
        self.overrides = overrides

    @classmethod
    def from_csv(cls, path: Path = DEFAULT_OVERRIDES) -> "QualityPolicy":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return cls([
            QualityOverride(
                document_id=int(row["document_id"]), file_name=row["file_name"],
                page_number=int(row["page_number"]) if row["page_number"] else None,
                field=row["field"], raw_value=row["raw_value"], review_status=row["review_status"],
                reviewed_value=row["reviewed_value"], reason=row["reason"], kg_usage_policy=row["kg_usage_policy"],
            )
            for row in rows
        ])

    def field_policy(self, document_id: int, field: str) -> QualityOverride | None:
        return next((item for item in self.overrides if item.document_id == document_id and item.page_number is None and item.field == field), None)

    def effective_field_value(self, document_id: int, field: str, raw_value: str) -> str:
        override = self.field_policy(document_id, field)
        if override is None or override.kg_usage_policy == "USE_RAW":
            return raw_value
        if override.kg_usage_policy == "USE_REVIEWED_OVERRIDE":
            return override.reviewed_value
        return ""

    def field_blocks_resolution(self, document_id: int, field: str) -> bool:
        override = self.field_policy(document_id, field)
        return bool(override and override.kg_usage_policy in {"DO_NOT_RESOLVE_FROM_THIS_FIELD", "REQUIRES_HUMAN_REVIEW"})

    def page_blocks_auto_verify(self, document_id: int, page_number: int) -> bool:
        return any(
            item.document_id == document_id and item.page_number == page_number
            and item.kg_usage_policy in {"DO_NOT_AUTO_VERIFY", "REQUIRES_HUMAN_REVIEW"}
            for item in self.overrides
        )
