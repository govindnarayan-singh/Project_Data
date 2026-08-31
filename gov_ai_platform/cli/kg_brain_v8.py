from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.graph.brain_v8 import acquire, analyze, finalize, ingest, prepare, run_benchmarks, run_kg


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="KG Brain V8 final bounded anchor closure")
    parser.add_argument("phase", choices=("prepare", "acquire", "ingest", "kg", "analyze", "benchmarks", "finalize"))
    phase = parser.parse_args().phase
    result = {
        "prepare": prepare,
        "acquire": acquire,
        "ingest": ingest,
        "kg": run_kg,
        "analyze": analyze,
        "benchmarks": run_benchmarks,
        "finalize": finalize,
    }[phase]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
