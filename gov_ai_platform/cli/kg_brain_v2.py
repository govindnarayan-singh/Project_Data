from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.graph.brain_v2 import analyze_graph, capture_baseline, finalize_report, retrieval_regression, run_kg_twice


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="KG Brain V2 deterministic workflow")
    parser.add_argument("action", choices=("baseline", "kg", "analyze", "retrieval", "finalize"))
    args = parser.parse_args()
    if args.action == "baseline":
        result = capture_baseline()
    elif args.action == "kg":
        result = run_kg_twice()
    elif args.action == "analyze":
        result = analyze_graph()
    elif args.action == "retrieval":
        result = retrieval_regression()
    else:
        result = finalize_report()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
