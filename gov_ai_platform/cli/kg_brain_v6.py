from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.kg_brain_v6 import KgBrainV6AcquisitionService
from knowledge_engine.graph.brain_v6 import (
    analyze_graph,
    capture_baseline,
    controlled_ingestion,
    retrieval_regression,
    run_kg_twice,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="KG Brain V6 official-source expansion work package")
    parser.add_argument(
        "phase",
        choices=("baseline", "freeze", "acquire", "ingest", "kg", "graph", "retrieval", "finish"),
    )
    args = parser.parse_args()
    if args.phase == "baseline":
        result = capture_baseline()
    elif args.phase == "freeze":
        rows, references = KgBrainV6AcquisitionService().freeze_targets()
        result = {"status": "PASS", "targets": len(rows), "references": len(references)}
    elif args.phase == "acquire":
        result = KgBrainV6AcquisitionService().run()
    elif args.phase == "ingest":
        result = controlled_ingestion()
    elif args.phase == "kg":
        result = run_kg_twice()
    elif args.phase == "graph":
        result = analyze_graph()
    elif args.phase == "retrieval":
        result = retrieval_regression()
    else:
        result = {
            "ingestion": controlled_ingestion(), "kg": run_kg_twice(),
            "graph": analyze_graph(), "retrieval": retrieval_regression(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
