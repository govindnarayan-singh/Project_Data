from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.acquisition import GraphGuidedAcquisitionService


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Build graph-guided local-PDF acquisition artifacts without ingestion")
    parser.add_argument("--target", type=int, default=120)
    args = parser.parse_args()
    result = GraphGuidedAcquisitionService().run(manifest_target=args.target)
    print(json.dumps({
        "match_statuses": result["match_statuses"], "manifest": result["manifest"],
        "expected_graph_impact": result["expected_graph_impact"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
