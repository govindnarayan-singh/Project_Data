from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.repository import ExternalAcquisitionRepository
from knowledge_engine.external_acquisition.service import ExternalAcquisitionService


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run controlled authoritative external acquisition V1")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    if args.migrate:
        ExternalAcquisitionRepository().apply_migration()
    result = ExternalAcquisitionService().run(limit=args.limit, live=not args.offline)
    print(json.dumps(
        {
            "run_id": result["run_id"],
            "network": result["network"],
            "metrics": result["metrics"],
            "target_states": result["target_states"],
            "manifest": result["manifest"],
            "integrity": result["integrity"],
            "inserted_catalogue_rows": result["inserted_catalogue_rows"],
            "external_catalogue_rows": result["external_catalogue_rows"],
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))


if __name__ == "__main__":
    main()
