from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.external_acquisition.kg_brain_v7 import KgBrainV7AcquisitionService
from knowledge_engine.graph.brain_v7 import (
    analyze_anchor_closure,
    build_closure_manifest,
    capture_and_freeze,
    classify_source_coverage,
    controlled_ingestion,
    reconcile_preingestion_local_aliases,
    run_frozen_benchmarks,
    run_kg_benchmark,
    run_kg_twice,
    validate_frozen_anchor,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="KG Brain V7 frozen-anchor first-hop closure")
    parser.add_argument(
        "phase",
        choices=("freeze", "coverage", "acquire", "manifest", "ingest", "kg", "analyze", "kg-benchmark", "benchmarks", "validate"),
    )
    phase = parser.parse_args().phase
    if phase == "freeze":
        result = capture_and_freeze()
    elif phase == "coverage":
        result = classify_source_coverage()
    elif phase == "acquire":
        result = KgBrainV7AcquisitionService().run()
    elif phase == "manifest":
        result = {"reconcile": reconcile_preingestion_local_aliases(), "manifest": build_closure_manifest()}
    elif phase == "ingest":
        result = controlled_ingestion()
    elif phase == "kg":
        result = run_kg_twice()
    elif phase == "analyze":
        result = analyze_anchor_closure()
    elif phase == "kg-benchmark":
        result = run_kg_benchmark()
    elif phase == "benchmarks":
        result = run_frozen_benchmarks()
    else:
        result = validate_frozen_anchor()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
