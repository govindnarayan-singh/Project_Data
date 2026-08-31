from __future__ import annotations

import argparse
import json
import sys

from knowledge_engine.retrieval.graph_aware import GraphAwareRetrievalService, GraphDirection, response_as_dict


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Experimental one-hop validated REFERENCES expansion")
    parser.add_argument("query")
    parser.add_argument("--direction", choices=[item.value for item in GraphDirection], default="bidirectional")
    parser.add_argument("--seed-top-k", type=int, choices=(1, 3, 5), default=3)
    parser.add_argument("--dense-top-k", type=int, default=10)
    parser.add_argument("--max-per-seed", type=int, default=10)
    parser.add_argument("--max-total", type=int, default=25)
    args = parser.parse_args()
    result = GraphAwareRetrievalService().search(
        args.query, direction=args.direction, seed_top_k=args.seed_top_k,
        dense_top_k=args.dense_top_k, max_graph_additions_per_seed=args.max_per_seed,
        max_graph_additions_total=args.max_total,
    )
    print(json.dumps(response_as_dict(result), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
