from __future__ import annotations

import argparse

from knowledge_engine.external_acquisition.kg_brain_v4 import KgBrainV4AcquisitionService
from knowledge_engine.graph.brain_v4 import (
    analyze_graph,
    capture_graph_baseline,
    controlled_ingestion_gate,
    retrieval_regression,
    run_kg_twice,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="KG Brain V4 official-source coverage and graph reconstruction")
    parser.add_argument("phase", choices=("baseline", "acquire", "ingestion-gate", "kg", "analyze", "retrieval"))
    args = parser.parse_args()
    if args.phase == "baseline":
        result = capture_graph_baseline()
    elif args.phase == "acquire":
        result = KgBrainV4AcquisitionService().run()
    elif args.phase == "ingestion-gate":
        result = controlled_ingestion_gate()
    elif args.phase == "kg":
        result = run_kg_twice()
    elif args.phase == "analyze":
        result = analyze_graph()
    else:
        result = retrieval_regression()
    print(result)


if __name__ == "__main__":
    main()
