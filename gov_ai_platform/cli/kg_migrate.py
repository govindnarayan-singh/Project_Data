from __future__ import annotations

import argparse
import json

from knowledge_engine.graph.repository import GraphRepository


REQUIRED_TABLES = {
    "kg_departments", "kg_department_aliases", "kg_extraction_runs",
    "kg_reference_candidates", "kg_relations", "kg_relation_evidence", "kg_reviews",
}


def check_migration(repository: GraphRepository) -> dict[str, object]:
    with repository.connect() as connection:
        connection.execute(repository.migration_sql())
        rows = connection.execute(
            """SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'kg_%'"""
        ).fetchall()
        tables = {str(row["tablename"]) for row in rows}
        missing = sorted(REQUIRED_TABLES - tables)
        constraints = int(connection.execute(
            """SELECT count(*) AS count FROM pg_constraint
                 WHERE conrelid IN ('kg_reference_candidates'::regclass,'kg_relations'::regclass)"""
        ).fetchone()["count"])
        indexes = int(connection.execute(
            """SELECT count(*) AS count FROM pg_indexes
                 WHERE schemaname='public' AND tablename IN ('kg_reference_candidates','kg_relations')"""
        ).fetchone()["count"])
        connection.rollback()
    if missing:
        raise RuntimeError(f"Migration check missing tables: {missing}")
    return {"status": "PASS", "rolled_back": True, "tables": sorted(tables), "constraints": constraints, "indexes": indexes}


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or apply additive KG V1 PostgreSQL migration")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repository = GraphRepository()
    if args.apply:
        repository.apply_migration()
        result = {"status": "APPLIED", "migration": "001_kg_v1.sql"}
    else:
        result = check_migration(repository)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
