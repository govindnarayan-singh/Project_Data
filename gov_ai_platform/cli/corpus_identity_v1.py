from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.corpus_identity.repository import CorpusIdentityRepository
from knowledge_engine.corpus_identity.service import CorpusIdentityService


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Build and resolve the persistent local PDF identity catalogue")
    parser.add_argument("--apply-migration", action="store_true", help="Apply additive migration 003 before indexing")
    parser.add_argument("--resume", action="store_true", help="Skip unchanged SHA/version catalogue rows")
    parser.add_argument("--workers", type=int, default=8, help="Inventory hashing/page-count workers")
    parser.add_argument("--migration-only", action="store_true", help="Apply/validate the schema without indexing")
    args = parser.parse_args()

    repository = CorpusIdentityRepository()
    if args.apply_migration:
        repository.apply_migration()
    migration = repository.migration_state()
    if migration["status"] != "PASS":
        raise RuntimeError("Migration 003 is required; rerun with --apply-migration after creating a PostgreSQL backup")
    if args.migration_only:
        print(json.dumps(migration, indent=2))
        return
    result = CorpusIdentityService(repository=repository).run(resume=args.resume, workers=args.workers)
    print(
        json.dumps(
            {
                "result": result["recommendation"],
                "catalogue_rows": result["catalogue"]["catalogue_rows"],
                "resolution_statuses": result["resolution_v2"]["statuses"],
                "manifest": result["manifest"],
                "next_recommendation": result["next_recommendation"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
