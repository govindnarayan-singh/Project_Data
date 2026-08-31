from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from knowledge_engine.graph.brain_v7 import (
    ANCHOR_TARGETS,
    MANIFEST,
    OUTPUT_DIR,
    classify_source_coverage,
)
from knowledge_engine.settings import PROJECT_ROOT

from . import kg_guided_v1 as engine
from .kg_brain_v4 import KgBrainV4AcquisitionService
from .kg_brain_v6 import KgBrainV6AcquisitionService
from .kg_guided_v1 import KgGuidedAcquisitionV1Service, _read_csv, _write_csv, _write_json
from .models import ReferenceIdentity
from .queries import assert_public_query, generate_queries
from .registry import classify_strategy


CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_brain_v7"


def configure_v7() -> None:
    engine.OUTPUT_DIR = OUTPUT_DIR
    engine.TARGET_SOURCE = ANCHOR_TARGETS
    engine.MANIFEST = MANIFEST
    engine.CACHE_DIR = CACHE_DIR


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


class KgBrainV7AcquisitionService(KgBrainV6AcquisitionService):
    """Run official acquisition only for the frozen V7 first-hop READY set."""

    def __init__(self, repository=None):
        configure_v7()
        KgBrainV4AcquisitionService.__init__(self, repository)
        configure_v7()

    @staticmethod
    def _adapter(definition):
        configure_v7()
        return KgBrainV6AcquisitionService._adapter(definition)

    def freeze_targets(self) -> tuple[list[dict[str, Any]], list[ReferenceIdentity]]:
        configure_v7()
        frozen = _read_csv(ANCHOR_TARGETS)
        statuses = {
            row["target_key"]: row
            for row in _read_csv(OUTPUT_DIR / "anchor_target_status_before.csv")
        }
        if len(frozen) != 709 or set(statuses) != {row["target_key"] for row in frozen}:
            raise RuntimeError("V7 frozen anchor target membership changed")
        ready = [
            statuses[row["target_key"]]
            for row in frozen
            if statuses[row["target_key"]]["actionable_state"] == "READY_FOR_OFFICIAL_ACQUISITION"
        ]
        ready.sort(key=lambda row: (-int(row["priority_score"]), row["target_key"]))

        source_rows: list[dict[str, Any]] = []
        references: list[ReferenceIdentity] = []
        quality_rows: list[dict[str, Any]] = []
        for rank, row in enumerate(ready, 1):
            dates = [value for value in _split(row.get("date/year", "")) if len(value) == 10]
            reference_date = None
            if len(dates) == 1:
                try:
                    reference_date = date.fromisoformat(dates[0])
                except ValueError:
                    reference_date = None
            mapped = {
                **row,
                "rank": rank,
                "normalized_target_identity": row["target_key"],
                "document_kind": row["kind"],
                "raw_variants": row["raw_variants"],
                "date": row.get("date/year", ""),
                "issuer": row.get("issuer/jurisdiction", ""),
                "distinct_citing_documents": row["distinct_anchor_citing_documents"],
                "mentions": row["mention_count"],
                "relation_signals": row["strong_relation_signal"],
                "component_bridge_score": row["bridge_potential"],
                "citing_components": row["citing_anchor_components"],
                "citing_document_ids": row["anchor_citing_document_ids"],
                "source_quality": row["source_quality"],
            }
            reference = ReferenceIdentity(
                target_key=row["target_key"], reference_kind=row["kind"],
                raw_identifier=row["raw_variants"] or row["canonical_identity"],
                normalized_identifier=row["canonical_identity"], reference_date=reference_date,
                issuer=row.get("issuer/jurisdiction", ""), mentions=int(row["mention_count"]),
                citing_documents=int(row["distinct_anchor_citing_documents"]),
                relation_signals=tuple(["SIGNAL"] * int(row["strong_relation_signal"])),
                priority_score=int(row["priority_score"]), priority_tier=row["priority_tier"],
                strategy=classify_strategy(row["kind"], row["canonical_identity"]),
                source_page_quality=row["source_quality"], local_resolution_state="READY_FOR_DISCOVERY",
            )
            try:
                for query in generate_queries(reference):
                    assert_public_query(query)
            except ValueError:
                continue
            mapped["rank"] = len(source_rows) + 1
            source_rows.append(mapped)
            references.append(reference)
            quality_rows.append({
                "rank": mapped["rank"], "target": row["target_key"],
                "quality_state": "READY_FOR_DISCOVERY",
                "reason": "Frozen anchor target passed parser, identity, availability and operational source gates",
            })

        self.references = references
        self.source_rows = source_rows
        self.quality_by_target = {row["target"]: row["quality_state"] for row in quality_rows}
        _write_csv(OUTPUT_DIR / "target_batch.csv", source_rows)
        _write_csv(OUTPUT_DIR / "target_quality.csv", quality_rows)
        _write_json(OUTPUT_DIR / "target_batch_sha.json", {
            "status": "PASS", "anchor_target_rows": len(frozen),
            "anchor_targets_sha256": hashlib.sha256(ANCHOR_TARGETS.read_bytes()).hexdigest(),
            "ready_first_hop_targets": len(ready),
            "target_batch_sha256": hashlib.sha256((OUTPUT_DIR / "target_batch.csv").read_bytes()).hexdigest(),
            "first_hop_only": True, "support_document_references_included": False,
        })
        return source_rows, references

    def run(self) -> dict[str, Any]:
        configure_v7()
        result = KgGuidedAcquisitionV1Service.run(self)
        result["target_count"] = len(self.source_rows)
        result["frozen_anchor_targets"] = 709
        result["first_hop_only"] = True
        result["second_hop_acquisition"] = 0
        _write_json(OUTPUT_DIR / "run_summary.json", result)
        network = self._network_outcomes(result["results"])
        coverage = classify_source_coverage(network)
        result["post_attempt_coverage"] = coverage
        _write_json(OUTPUT_DIR / "run_summary.json", result)
        return result


__all__ = ["CACHE_DIR", "KgBrainV7AcquisitionService", "configure_v7"]
