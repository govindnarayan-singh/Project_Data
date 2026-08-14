from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.v2_1 import ExternalAcquisitionV21Service


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Replay the frozen External Acquisition V2 batch with V2.1 hardening")
    parser.add_argument("action", choices=("baseline", "run", "replay", "finalize"))
    parser.add_argument("--network-unavailable", action="store_true")
    args = parser.parse_args()
    service = ExternalAcquisitionV21Service()
    if args.action == "baseline":
        result = service.baseline()
    elif args.action in {"run", "replay"}:
        result = service.run(replay=args.action == "replay", public_network_available=not args.network_unavailable)
    else:
        result = service.finalize_v2_1()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status", result.get("outcome")) not in {"FAIL", None} else 1


if __name__ == "__main__":
    raise SystemExit(main())
