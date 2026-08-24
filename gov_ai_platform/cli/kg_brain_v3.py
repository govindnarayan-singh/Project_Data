from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.graph.brain_v3 import analyze_graph, capture_baseline, finalize_report, retrieval_regression, run_kg_twice, sync_acquisition


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="KG Brain V3 deterministic workflow")
    parser.add_argument("action", choices=("baseline", "acquisition", "kg", "analyze", "retrieval", "finalize"))
    parser.add_argument("--passed", type=int, default=0)
    parser.add_argument("--failed", type=int, default=0)
    parser.add_argument("--skipped", type=int, default=0)
    args = parser.parse_args()
    action = args.action
    if action == "finalize":
        result = finalize_report(passed=args.passed, failed=args.failed, skipped=args.skipped)
    else:
        result = {"baseline": capture_baseline, "acquisition": sync_acquisition, "kg": run_kg_twice,
                  "analyze": analyze_graph, "retrieval": retrieval_regression}[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
