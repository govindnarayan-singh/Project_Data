from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Controlled External Acquisition V2")
    parser.add_argument("action", choices=("baseline", "migrate", "backfill", "freeze", "run", "replay", "integrity", "finalize"))
    parser.add_argument("--passed", default="PENDING")
    parser.add_argument("--failed", default="PENDING")
    parser.add_argument("--skipped", default="PENDING")
    parser.add_argument("--postgresql-integration", default="PENDING")
    parser.add_argument("--django", default="PENDING")
    parser.add_argument("--compilation", default="PENDING")
    parser.add_argument("--git-diff-check", default="PENDING")
    args = parser.parse_args()
    service = ExternalAcquisitionV2Service()
    if args.action == "baseline":
        value = service.baseline()
    elif args.action == "migrate":
        value = service.apply_migration()
    elif args.action == "backfill":
        value = service.backfill_previous_documents()
    elif args.action == "freeze":
        _, value = service.freeze_targets()
    elif args.action == "run":
        value = service.run()
    elif args.action == "replay":
        value = service.run(replay=True)
    elif args.action == "finalize":
        value = service.finalize({
            "passed": args.passed, "failed": args.failed, "skipped": args.skipped,
            "postgresql_integration": args.postgresql_integration,
            "django": args.django, "compilation": args.compilation,
            "git_diff_check": args.git_diff_check,
        })
    else:
        value = service.archive_integrity()
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
