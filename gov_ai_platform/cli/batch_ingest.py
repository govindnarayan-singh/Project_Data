from __future__ import annotations

import argparse
import sys
from pathlib import Path

from knowledge_engine.pipelines.batch_ingest import (
    ManifestValidationError,
    execute_manifest,
    manifest_sha256,
    parse_manifest,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Manifest-based GR ingestion with restartable structured results"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan only; do not extract, embed, or write PostgreSQL")
    parser.add_argument("--resume", action="store_true", help="Resume the named run, or the latest compatible incomplete run")
    parser.add_argument("--force", action="store_true", help="Reprocess existing documents even when their SHA-256 is unchanged")
    parser.add_argument("--run-id", help="Explicit run ID; required to select a particular resume run")
    args = parser.parse_args()
    if args.resume and args.force:
        parser.error("--resume and --force cannot be combined")

    try:
        entries = parse_manifest(args.manifest)
        digest = manifest_sha256(args.manifest.resolve())
    except (OSError, ManifestValidationError, ValueError) as error:
        parser.error(str(error))

    print("FROZEN MANIFEST")
    print(f"Path: {args.manifest.resolve()}")
    print(f"SHA-256: {digest}")
    print(f"Documents: {len(entries)}")
    for entry in entries:
        print(f"{entry.row_number - 1:02d}. {entry.file_path} | {entry.department}")
    print()

    def report(result) -> None:
        print(
            f"{result.file_name} | {result.operation} | {result.status} | "
            f"pages={result.pages_total} chunks={result.chunks_created} "
            f"elapsed={result.elapsed_seconds:.2f}s"
        )

    try:
        run_dir, _results, summary = execute_manifest(
            args.manifest,
            run_id=args.run_id,
            dry_run=args.dry_run,
            resume=args.resume,
            force=args.force,
            on_result=report,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(f"\nOutputs: {run_dir}")
    print(f"Statuses: {summary['status_counts']}")
    if summary["failure_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
