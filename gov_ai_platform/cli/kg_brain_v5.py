from __future__ import annotations

import argparse
import json

from knowledge_engine.external_acquisition.kg_brain_v5 import KgBrainV5AcquisitionService
from knowledge_engine.graph.brain_v5 import (
    analyze_graph,
    analyze_targets,
    capture_baseline,
    controlled_ingestion_gate,
    retrieval_regression,
    run_kg_twice,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="KG Brain V5 deterministic parser and closure work package")
    parser.add_argument("phase", choices=("baseline", "analyze", "parser", "acquire", "ingest", "kg", "graph", "retrieval", "finish"))
    args = parser.parse_args()
    if args.phase == "baseline":
        result = capture_baseline()
    elif args.phase in {"analyze", "parser"}:
        result = analyze_targets()
    elif args.phase == "acquire":
        result = KgBrainV5AcquisitionService().run()
    elif args.phase == "ingest":
        result = controlled_ingestion_gate()
    elif args.phase == "kg":
        result = run_kg_twice()
    elif args.phase == "graph":
        result = analyze_graph()
    elif args.phase == "retrieval":
        result = retrieval_regression()
    else:
        result = {
            "ingestion": controlled_ingestion_gate(),
            "kg": run_kg_twice(),
            "graph": analyze_graph(),
            "retrieval": retrieval_regression(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
