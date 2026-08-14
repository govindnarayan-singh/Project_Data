from __future__ import annotations

import argparse
import json

from knowledge_engine.external_acquisition.repository import ExternalAcquisitionRepository
from knowledge_engine.external_acquisition.service import ExternalAcquisitionService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run controlled authoritative external acquisition V1")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    if args.migrate:
        ExternalAcquisitionRepository().apply_migration()
    print(json.dumps(ExternalAcquisitionService().run(limit=args.limit, live=not args.offline), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
