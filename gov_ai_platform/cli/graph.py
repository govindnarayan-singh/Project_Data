from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from knowledge_engine.graph.analytics import administrative_effects, topology
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.traversal import GraphTraversalService


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Inspect the validated PostgreSQL administrative graph")
    sub = parser.add_subparsers(dest="command", required=True)
    document = sub.add_parser("document"); document.add_argument("document_id", type=int)
    for name in ("upstream", "downstream", "history"):
        item = sub.add_parser(name); item.add_argument("document_id", type=int); item.add_argument("--depth", type=int, default=3)
    path = sub.add_parser("path"); path.add_argument("source_id", type=int); path.add_argument("target_id", type=int); path.add_argument("--depth", type=int, default=5)
    sub.add_parser("metrics")
    args = parser.parse_args(); service = GraphTraversalService(); repository = GraphRepository()
    if args.command == "document":
        with repository.connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id=%s", (args.document_id,)).fetchone()
        if row is None: raise SystemExit(f"Document {args.document_id} does not exist")
        result = {"document": dict(row), "issued_by": repository.get_issued_by(args.document_id),
                  "outgoing": repository.get_outgoing_references(args.document_id),
                  "incoming": repository.get_incoming_references(args.document_id),
                  "unresolved": repository.get_unresolved_references(args.document_id),
                  "relation_signals": repository.get_relation_signals(args.document_id),
                  "administrative_effects": administrative_effects(args.document_id, repository)}
    elif args.command == "upstream": result = asdict(service.upstream(args.document_id, args.depth))
    elif args.command == "downstream": result = asdict(service.downstream(args.document_id, args.depth))
    elif args.command == "path": result = service.paths(args.source_id, args.target_id, args.depth)
    elif args.command == "history": result = service.history(args.document_id, args.depth)
    else: result = topology(repository)[0]
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
