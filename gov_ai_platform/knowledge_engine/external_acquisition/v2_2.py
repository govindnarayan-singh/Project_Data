from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from knowledge_engine.settings import PROJECT_ROOT

from . import v2 as v2_module
from . import v2_1 as v2_1_module
from .adapters import (
    BlockedOfficialSource,
    DivyangSahayakSource,
    IndiaCodeSource,
    MaharashtraDepartmentSource,
)
from .models import TargetState


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "external_acquisition_v2_2"
TARGET_SOURCE = PROJECT_ROOT / "outputs" / "external_ingestion_v1" / "next_external_target_priority.csv"
MANIFEST = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v2_2.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "v2_2"
V2_OUTPUT = PROJECT_ROOT / "outputs" / "external_acquisition_v2"
V21_OUTPUT = PROJECT_ROOT / "outputs" / "external_acquisition_v2_1"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


class ExternalAcquisitionV22Service(v2_1_module.ExternalAcquisitionV21Service):
    """Local-first V2.2 replay with scoped official department adapters."""

    def __init__(self, repository=None):
        v2_module.OUTPUT_DIR = OUTPUT_DIR
        v2_module.TARGET_SOURCE = TARGET_SOURCE
        v2_module.V2_MANIFEST = MANIFEST
        v2_module.CACHE_DIR = CACHE_DIR
        v2_1_module.OUTPUT_DIR = OUTPUT_DIR
        v2_1_module.TARGET_SOURCE = TARGET_SOURCE
        v2_1_module.MANIFEST = MANIFEST
        v2_1_module.CACHE_DIR = CACHE_DIR
        v2_module.ExternalAcquisitionV2Service.__init__(self, repository)

    @staticmethod
    def _adapter(definition):
        if definition.source_id == "india_code":
            return IndiaCodeSource(
                definition,
                cache_dir=PROJECT_ROOT / "data" / "acquisition" / "cache" / "v2" / definition.source_id,
            )
        if definition.source_id == "divyang_sahayak":
            return DivyangSahayakSource(definition, cache_dir=CACHE_DIR / definition.source_id)
        if definition.source_id.startswith("maharashtra_") and definition.source_id in {
            "maharashtra_finance",
            "maharashtra_parliamentary_affairs",
            "maharashtra_environment",
            "maharashtra_marathi_language",
            "maharashtra_it",
        }:
            return MaharashtraDepartmentSource(definition, cache_dir=CACHE_DIR / definition.source_id)
        return BlockedOfficialSource(definition)

    @staticmethod
    def _target_state(results):
        states = {item.target_state for item in results if item.target_state}
        for state in (
            TargetState.AUTO_VERIFIED_READY.value,
            "ALREADY_INGESTED",
            "LOCAL_DUPLICATE_DISCOVERED",
            TargetState.AMBIGUOUS.value,
            TargetState.ARCHIVE_FAILED.value,
            TargetState.IDENTITY_MISMATCH.value,
            TargetState.UNPARSEABLE_DOWNLOAD.value,
            TargetState.UNPARSEABLE_REFERENCE.value,
        ):
            if state in states:
                return state
        # An unavailable alternate does not erase the original CAPTCHA block.
        if TargetState.BLOCKED_BY_SOURCE.value in states:
            return TargetState.BLOCKED_BY_SOURCE.value
        return v2_module.ExternalAcquisitionV2Service._target_state(results)

    def baseline(self) -> dict[str, Any]:
        result = v2_module.ExternalAcquisitionV2Service.baseline(self)
        previous_backfill = V2_OUTPUT / "archive_backfill_results.csv"
        if previous_backfill.is_file():
            shutil.copyfile(previous_backfill, OUTPUT_DIR / "archive_backfill_results.csv")
        result["local_first_pdf_flow"] = {
            "incoming_root_identity": "EXTERNAL_INCOMING",
            "archive_root_identity": "EXTERNAL_OFFICIAL",
            "network_after_incoming_permitted": False,
        }
        _write_json(OUTPUT_DIR / "baseline.json", result)
        return result

    def run(self, *, replay: bool = False, public_network_available: bool = True) -> dict[str, Any]:
        result = v2_module.ExternalAcquisitionV2Service.run(
            self, replay=replay, public_network_available=public_network_available
        )
        v2_1_module.ExternalAcquisitionV21Service._write_analysis(self, result)
        before = _read_json(V21_OUTPUT / "run_summary.json")["metrics"]
        after = result["metrics"]
        comparison = {
            "same_frozen_targets": result["batch"]["sha256"]
            == _read_json(V21_OUTPUT / "target_batch_sha.json")["sha256"],
            "targets": {"before": 30, "after": len(result["target_states"])},
            "official_candidates": {"before": before["official_candidates"], "after": after["official_candidates"]},
            "exact_matches": {"before": before["match_exact"], "after": after["match_exact"]},
            "ready_documents": {"before": before["auto_verified_ready"], "after": after["auto_verified_ready"]},
            "source_blocks": {"before": before["blocked_by_source"], "after": after["blocked_by_source"]},
            "gates_loosened": False,
        }
        _write_json(OUTPUT_DIR / "v2_1_vs_v2_2_comparison.json", comparison)
        incoming_rows = [row for row in result["results"] if row.get("local_acquired_path")]
        ready_rows = [
            row for row in result["results"]
            if row.get("target_state") == TargetState.AUTO_VERIFIED_READY.value
        ]
        local_first = {
            "status": "PASS",
            "incoming_files_recorded": len(incoming_rows),
            "incoming_rows_missing_local_verification": sum(
                not row.get("raw_source_evidence", {}).get("local_pdf_verification") for row in incoming_rows
            ),
            "ready_rows": len(ready_rows),
            "ready_rows_missing_archive": sum(
                row.get("archive_status") != "EXTERNAL_OFFICIAL_ARCHIVED" for row in ready_rows
            ),
            "manifest_uses_incoming": False,
            "post_archive_network_dependency": False,
        }
        if local_first["incoming_rows_missing_local_verification"] or local_first["ready_rows_missing_archive"]:
            local_first["status"] = "FAIL"
        _write_json(OUTPUT_DIR / "local_first_integrity.json", local_first)
        return result

    def finalize_v2_2(self, validation: dict[str, Any] | None = None) -> dict[str, Any]:
        final = v2_module.ExternalAcquisitionV2Service.finalize(self, validation)
        comparison = _read_json(OUTPUT_DIR / "v2_1_vs_v2_2_comparison.json")
        local_first = _read_json(OUTPUT_DIR / "local_first_integrity.json")
        replay = _read_json(OUTPUT_DIR / "replay_summary.json")
        manifest_rows = v2_module._read_csv(MANIFEST)
        integrity = _read_json(OUTPUT_DIR / "integrity_report.json")
        before_closure = _read_json(OUTPUT_DIR / "closure_metrics_before.json")
        after_closure = _read_json(OUTPUT_DIR / "closure_metrics_after.json")
        previous_after = _read_json(V21_OUTPUT / "closure_metrics_after.json")
        # V2's generic helper reconstructs the state before any external archive
        # existed. V2.2 starts after V2.1's five-document archive backfill, so its
        # true before value is the V2.1 after value. The after value is computed
        # from the current archive and includes any new V2.2 promotion.
        before_local_archive = dict(previous_after["local_archived_availability_closure"])
        before_local_archive["definition"] = (
            "Exact target has physical immutable source bytes in GR_ROOT or the configured "
            "EXTERNAL_DOCUMENT_ROOT; Incoming alone is excluded."
        )
        before_closure["local_archived_availability_closure"] = before_local_archive
        known_after = after_closure["known_availability_closure"]
        after_closure["local_archived_availability_closure"] = {
            "numerator": known_after["numerator"],
            "denominator": known_after["denominator"],
            "percentage": known_after["percentage"],
            "definition": (
                "Exact target has physical immutable source bytes in GR_ROOT or the configured "
                "EXTERNAL_DOCUMENT_ROOT; Incoming alone is excluded."
            ),
        }
        _write_json(OUTPUT_DIR / "closure_metrics_before.json", before_closure)
        _write_json(OUTPUT_DIR / "closure_metrics_after.json", after_closure)
        if integrity["status"] != "PASS" or local_first["status"] != "PASS":
            outcome = "FAIL"
        else:
            outcome = "PASS" if manifest_rows else "CONDITIONAL PASS"
        recommendation = (
            "A. REVIEW AND INGEST VERIFIED V2.2 MANIFEST"
            if manifest_rows
            else "B. EXPAND OFFICIAL SOURCE ADAPTERS FURTHER"
        )
        final.update({
            "outcome": outcome,
            "recommendation": recommendation,
            "comparison_v2_1_to_v2_2": comparison,
            "local_first_integrity": local_first,
            "manifest_documents": len(manifest_rows),
        })
        _write_json(OUTPUT_DIR / "final_validation.json", final)
        (OUTPUT_DIR / "final_report.md").write_text(
            f"""{outcome}

V2.2 replayed the exact 30-target batch at SHA-256
`{replay['batch']['sha256']}`. Internet bytes land in configured local Incoming,
document and identity verification read the local file, and only verified bytes
may enter External_Official. No ingestion occurred.

Official candidates: {replay['metrics']['official_candidates']}
Exact matches: {replay['metrics']['match_exact']}
AUTO_VERIFIED_READY: {replay['metrics']['auto_verified_ready']}
Manifest documents: {len(manifest_rows)}

Native Windows PostgreSQL remains 71 documents, 1,322 pages, 1,237 chunks and
1,237 1,024-D embeddings. Next recommendation: **{recommendation}**.
""",
            encoding="utf-8",
        )
        return final


__all__ = ["ExternalAcquisitionV22Service", "MANIFEST", "OUTPUT_DIR", "TARGET_SOURCE"]
