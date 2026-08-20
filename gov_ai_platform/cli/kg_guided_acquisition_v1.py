from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.kg_guided_v1 import KgGuidedAcquisitionV1Service


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run KG-guided local-first acquisition V1")
    parser.add_argument("action", choices=("baseline", "run", "replay", "integrity"))
    args = parser.parse_args()
    service = KgGuidedAcquisitionV1Service()
    result = getattr(service, args.action)()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
