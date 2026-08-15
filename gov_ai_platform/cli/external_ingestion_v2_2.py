from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from knowledge_engine.external_ingestion.v2_2 import MANIFEST, V22ExternalIngestionService
from knowledge_engine.pipelines.batch_ingest import BatchIngestor, BatchResult
from knowledge_engine.settings import PROJECT_ROOT


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v2"


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def validate(*, allow_ingested: bool = False) -> tuple[dict[str, Any], list]:
    service = V22ExternalIngestionService()
    decisions, entries = service.validate_manifest(MANIFEST, allow_ingested=allow_ingested)
    report = {
        "status": "PASS" if len(decisions) == 1 and len(entries) == 1 else (
            "PASS" if allow_ingested and len(decisions) == 1 and decisions[0].collision == "EXACT_DUPLICATE" else "FAIL"
        ),
        "manifest_path": MANIFEST.relative_to(PROJECT_ROOT).as_posix(),
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "row_count": len(decisions),
        "eligible": sum(item.eligible for item in decisions),
        "ineligible": sum(not item.eligible for item in decisions),
        "permanent_archive_entries": len(entries),
        "incoming_entries": sum("Incoming" in str(entry.absolute_path) for entry in entries),
        "rows": service.decision_rows(decisions),
    }
    _json(OUTPUT_DIR / "v2_2_manifest_validation.json", report)
    return report, entries


def run_ingestion(*, dry_run: bool = False, replay: bool = False) -> dict[str, Any]:
    report, entries = validate(allow_ingested=replay)
    if len(entries) != 1:
        raise RuntimeError(f"Exactly one eligible archived document required, found {len(entries)}")
    ingestor = BatchIngestor()
    if not dry_run:
        ingestor.preflight(entries)
    before = {entry.file_path: hashlib.sha256(entry.absolute_path.read_bytes()).hexdigest() for entry in entries}
    run_id = "kg-brain-v2-v22-replay" if replay else (
        "kg-brain-v2-v22-dry-run" if dry_run else "kg-brain-v2-v22-ingest"
    )
    results = [
        ingestor.process_entry(entry, run_id=run_id, dry_run=dry_run)
        for entry in entries
    ]
    after = {entry.file_path: hashlib.sha256(entry.absolute_path.read_bytes()).hexdigest() for entry in entries}
    changed = [path for path in before if before[path] != after[path]]
    rows = [asdict(item) for item in results]
    payload = {
        "status": "PASS" if not changed and all(
            item.status in {"DRY_RUN", "SUCCESS", "SUCCESS_WITH_WARNING", "SKIPPED_UNCHANGED"}
            for item in results
        ) else "FAIL",
        "dry_run": dry_run,
        "replay": replay,
        "manifest": report,
        "status_counts": dict(Counter(item.status for item in results)),
        "operation_counts": dict(Counter(item.operation for item in results)),
        "source_archive_sha_unchanged": not changed,
        "changed_archive_paths": changed,
        "results": rows,
    }
    output = OUTPUT_DIR / ("v2_2_ingestion_result.json" if not replay else "v2_2_ingestion_replay.json")
    if dry_run:
        output = OUTPUT_DIR / "v2_2_ingestion_dry_run.json"
    _json(output, payload)
    _csv(output.with_suffix(".csv"), rows, BatchResult.fieldnames())
    if payload["status"] != "PASS":
        raise RuntimeError(f"V2.2 ingestion failed: {payload}")
    return payload


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Ingest exactly the frozen local-first V2.2 archive manifest")
    parser.add_argument("action", choices=("reconcile", "validate", "dry-run", "ingest", "replay"))
    args = parser.parse_args()
    if args.action == "reconcile":
        result = V22ExternalIngestionService().reconcile_frozen_provenance()
        _json(OUTPUT_DIR / "v2_2_provenance_reconciliation.json", result)
    elif args.action == "validate":
        result = validate()[0]
    elif args.action == "dry-run":
        result = run_ingestion(dry_run=True)
    elif args.action == "ingest":
        result = run_ingestion()
    else:
        result = run_ingestion(replay=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
