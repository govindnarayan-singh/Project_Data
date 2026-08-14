from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from knowledge_engine.settings import PROJECT_ROOT, settings

from .models import CATALOG_VERSION, IdentityRecord, LocalResolution


MIGRATION_PATH = PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "003_local_corpus_identity_v1.sql"


class CorpusIdentityRepository:
    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def migration_sql(self) -> str:
        return MIGRATION_PATH.read_text(encoding="utf-8")

    def apply_migration(self) -> None:
        with self.connect() as connection:
            with connection.transaction():
                connection.execute(self.migration_sql())

    def migration_state(self) -> dict[str, Any]:
        with self.connect() as connection:
            table = connection.execute("SELECT to_regclass('public.local_document_catalog') AS name").fetchone()["name"]
            columns = {
                row["column_name"]
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='kg_reference_candidates'"
                )
            }
            indexes = {
                row["indexname"]
                for row in connection.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='local_document_catalog'")
            }
        required_columns = {
            "resolved_target_catalog_id",
            "local_resolution_status",
            "local_resolution_method",
            "local_resolution_evidence",
            "local_validation_status",
            "local_ingestion_eligibility",
        }
        required_indexes = {
            "idx_local_catalog_number",
            "idx_local_catalog_number_date",
            "idx_local_catalog_number_department",
            "idx_local_catalog_legal_key",
        }
        return {
            "table_present": table == "local_document_catalog",
            "candidate_columns_present": required_columns <= columns,
            "resolver_indexes_present": required_indexes <= indexes,
            "status": "PASS" if table == "local_document_catalog" and required_columns <= columns and required_indexes <= indexes else "FAIL",
        }

    def existing_catalogue(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            if connection.execute("SELECT to_regclass('public.local_document_catalog') AS name").fetchone()["name"] is None:
                return {}
            rows = connection.execute(
                "SELECT id,relative_path,sha256,file_size,file_mtime_ns,catalog_version,identity_parse_status FROM local_document_catalog"
            ).fetchall()
        return {str(row["relative_path"]).casefold(): dict(row) for row in rows}

    def ingested_identities(self) -> tuple[dict[str, int], dict[str, int]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id,file_path,sha256 FROM documents").fetchall()
        by_path = {str(row["file_path"]).replace("\\", "/").casefold(): int(row["id"]) for row in rows}
        by_sha = {str(row["sha256"]): int(row["id"]) for row in rows}
        return by_path, by_sha

    def reconcile_catalogue_ingested_links(self) -> dict[str, int]:
        """Refresh catalogue-to-search-document links without re-probing PDFs."""
        with self.connect() as connection:
            catalogue = connection.execute(
                "SELECT id,relative_path,sha256,ingested_document_id FROM local_document_catalog ORDER BY id"
            ).fetchall()
            documents = connection.execute(
                "SELECT id,file_path,sha256 FROM documents ORDER BY id"
            ).fetchall()
            by_path = {
                str(row["file_path"]).replace("\\", "/").casefold(): int(row["id"])
                for row in documents
            }
            by_sha = {str(row["sha256"]): int(row["id"]) for row in documents}
            updates = 0
            with connection.transaction():
                for row in catalogue:
                    path_target = by_path.get(str(row["relative_path"]).replace("\\", "/").casefold())
                    sha_target = by_sha.get(str(row["sha256"]))
                    if path_target is not None and sha_target is not None and path_target != sha_target:
                        raise ValueError(
                            f"Catalogue row {row['id']} path and SHA-256 resolve to different documents"
                        )
                    target = path_target if path_target is not None else sha_target
                    if row["ingested_document_id"] != target:
                        connection.execute(
                            "UPDATE local_document_catalog SET ingested_document_id=%s,updated_at=now() WHERE id=%s",
                            (target, int(row["id"])),
                        )
                        updates += 1
        return {
            "catalogue_rows": len(catalogue),
            "ingested_documents": len(documents),
            "updated_links": updates,
            "linked_catalogue_rows": sum(
                (by_path.get(str(row["relative_path"]).replace("\\", "/").casefold())
                 or by_sha.get(str(row["sha256"]))) is not None
                for row in catalogue
            ),
        }

    def upsert_record(self, record: IdentityRecord, connection=None) -> tuple[int, bool]:
        own_connection = connection is None
        active = connection or self.connect()
        values = asdict(record)
        values.pop("elapsed_seconds", None)
        values["probe_pages"] = list(values["probe_pages"])
        values["ocr_pages"] = list(values["ocr_pages"])
        fields = list(values)
        placeholders = ",".join(f"%({field})s" for field in fields)
        updates = ",".join(
            f"{field}=EXCLUDED.{field}" for field in fields if field not in {"relative_path", "created_at"}
        )
        sql = f"""
            INSERT INTO local_document_catalog({','.join(fields)}) VALUES ({placeholders})
            ON CONFLICT (relative_path) DO UPDATE SET {updates},updated_at=now()
            RETURNING id,(xmax=0) AS inserted
        """
        try:
            row = active.execute(sql, values).fetchone()
            if own_connection:
                active.commit()
            return int(row["id"]), bool(row["inserted"])
        finally:
            if own_connection:
                active.close()

    def catalogue_rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM local_document_catalog ORDER BY relative_path").fetchall()
        return [dict(row) for row in rows]

    def persist_resolution(self, resolution: LocalResolution, connection=None) -> int:
        target = resolution.catalog_id if resolution.status in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"} else None
        own_connection = connection is None
        active = connection or self.connect()
        try:
            context = active.transaction() if own_connection else nullcontext()
            with context:
                row = active.execute(
                    """
                    UPDATE kg_reference_candidates SET
                        resolved_target_catalog_id=%s,
                        local_resolution_status=%s,
                        local_resolution_method=%s,
                        local_resolution_evidence=%s,
                        local_resolution_candidate_count=%s,
                        local_validation_status=%s,
                        local_ingestion_eligibility=%s,
                        local_resolved_at=now()
                    WHERE id=ANY(%s) AND (
                        resolved_target_catalog_id IS DISTINCT FROM %s OR
                        local_resolution_status IS DISTINCT FROM %s OR
                        local_resolution_method IS DISTINCT FROM %s OR
                        local_resolution_evidence IS DISTINCT FROM %s OR
                        local_resolution_candidate_count IS DISTINCT FROM %s OR
                        local_validation_status IS DISTINCT FROM %s OR
                        local_ingestion_eligibility IS DISTINCT FROM %s
                    )
                    """,
                    (
                        target,
                        resolution.status,
                        resolution.method,
                        resolution.evidence,
                        len(resolution.candidate_catalog_ids),
                        resolution.validation_status,
                        resolution.ingestion_eligibility,
                        list(resolution.source_candidate_ids),
                        target,
                        resolution.status,
                        resolution.method,
                        resolution.evidence,
                        len(resolution.candidate_catalog_ids),
                        resolution.validation_status,
                        resolution.ingestion_eligibility,
                    ),
                )
            if own_connection:
                active.commit()
            return row.rowcount
        finally:
            if own_connection:
                active.close()

    def local_candidate_rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,s.file_name AS source_file,s.department AS source_department,
                       t.relative_path AS target_relative_path,t.document_kind AS target_document_kind,
                       t.normalized_document_number AS target_normalized_document_number,
                       t.normalized_date AS target_normalized_date,t.department_folder AS target_department,
                       t.identity_quality AS target_identity_quality,t.ingested_document_id AS target_ingested_document_id
                FROM kg_reference_candidates c
                JOIN documents s ON s.id=c.source_document_id
                LEFT JOIN local_document_catalog t ON t.id=c.resolved_target_catalog_id
                ORDER BY c.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_ingested_targets(
        self, resolutions: list[LocalResolution], catalogue_rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Promote local resolutions whose targets already have real document rows."""
        catalogue = {int(row["id"]): row for row in catalogue_rows}
        updated_candidates = inserted_relations = inserted_evidence = 0
        with self.connect() as connection:
            with connection.transaction():
                for resolution in resolutions:
                    if resolution.catalog_id is None or resolution.status not in {
                        "RESOLVED_LOCAL_EXACT",
                        "RESOLVED_LOCAL_STRONG",
                    }:
                        continue
                    target_document_id = catalogue[resolution.catalog_id].get("ingested_document_id")
                    if target_document_id is None:
                        continue
                    graph_status = "RESOLVED_EXACT" if resolution.status == "RESOLVED_LOCAL_EXACT" else "RESOLVED_STRONG"
                    eligible = connection.execute(
                        """
                        SELECT id,source_document_id FROM kg_reference_candidates
                        WHERE id=ANY(%s) AND parser_status='VALID'
                          AND NOT blocked_by_page_quality AND NOT blocked_by_target_metadata
                          AND source_document_id<>%s
                        ORDER BY id
                        """,
                        (list(resolution.source_candidate_ids), int(target_document_id)),
                    ).fetchall()
                    if not eligible:
                        continue
                    changed = connection.execute(
                        """
                        UPDATE kg_reference_candidates SET
                            resolution_status=%s,resolved_target_document_id=%s,
                            resolution_method=%s,resolution_evidence=%s,
                            resolution_candidate_count=1,confidence_category='HIGH',
                            validation_status='AUTO_VERIFIED',updated_at=now()
                        WHERE id=ANY(%s) AND (
                            resolution_status IS DISTINCT FROM %s OR
                            resolved_target_document_id IS DISTINCT FROM %s OR
                            resolution_method IS DISTINCT FROM %s OR
                            validation_status IS DISTINCT FROM 'AUTO_VERIFIED'
                        )
                        """,
                        (
                            graph_status,int(target_document_id),f"LOCAL_CATALOG_{resolution.method}",
                            resolution.evidence,[int(row["id"]) for row in eligible],graph_status,
                            int(target_document_id),f"LOCAL_CATALOG_{resolution.method}",
                        ),
                    )
                    updated_candidates += changed.rowcount
                    for candidate in eligible:
                        relation = connection.execute(
                            """
                            INSERT INTO kg_relations(source_document_id,target_document_id,relation_type,
                                                     validation_status,created_from_candidate_id)
                            VALUES (%s,%s,'REFERENCES','AUTO_VERIFIED',%s)
                            ON CONFLICT DO NOTHING RETURNING id
                            """,
                            (candidate["source_document_id"], int(target_document_id), candidate["id"]),
                        ).fetchone()
                        if relation:
                            relation_id = int(relation["id"])
                            inserted_relations += 1
                        else:
                            relation_id = int(connection.execute(
                                """SELECT id FROM kg_relations
                                   WHERE source_document_id=%s AND target_document_id=%s AND relation_type='REFERENCES'""",
                                (candidate["source_document_id"], int(target_document_id)),
                            ).fetchone()["id"])
                        evidence = connection.execute(
                            """INSERT INTO kg_relation_evidence(relation_id,candidate_id)
                               VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                            (relation_id, candidate["id"]),
                        ).fetchone()
                        inserted_evidence += int(evidence is not None)
        return {
            "updated_candidates": updated_candidates,
            "inserted_reference_relations": inserted_relations,
            "inserted_evidence_links": inserted_evidence,
            "human_confirmed_created": 0,
        }

    def integrity(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = dict(
                connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM local_document_catalog) catalogue_rows,
                      (SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM local_document_catalog GROUP BY relative_path HAVING count(*)>1) x) duplicate_paths,
                      (SELECT count(*) FROM local_document_catalog WHERE length(sha256)<>64) invalid_sha,
                      (SELECT count(*) FROM local_document_catalog c LEFT JOIN documents d ON d.id=c.ingested_document_id WHERE c.ingested_document_id IS NOT NULL AND d.id IS NULL) broken_ingested_links,
                      (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN local_document_catalog t ON t.id=c.resolved_target_catalog_id WHERE c.resolved_target_catalog_id IS NOT NULL AND t.id IS NULL) broken_catalog_links,
                      (SELECT count(*) FROM kg_reference_candidates WHERE local_resolution_status IN ('RESOLVED_LOCAL_EXACT','RESOLVED_LOCAL_STRONG') AND resolved_target_catalog_id IS NULL) resolved_without_catalog_target,
                      (SELECT count(*) FROM kg_reference_candidates WHERE local_validation_status='AUTO_VERIFIED' AND local_resolution_status NOT IN ('RESOLVED_LOCAL_EXACT','RESOLVED_LOCAL_STRONG')) invalid_auto_verified,
                      (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed,
                      (SELECT count(*) FROM documents) documents,
                      (SELECT count(*) FROM pages) pages,
                      (SELECT count(*) FROM chunks) chunks,
                      (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings
                    """
                ).fetchone()
            )
        numeric = {key: int(value) for key, value in row.items()}
        invariant_keys = (
            "duplicate_paths",
            "invalid_sha",
            "broken_ingested_links",
            "broken_catalog_links",
            "resolved_without_catalog_target",
            "invalid_auto_verified",
            "human_confirmed",
        )
        numeric["catalog_version"] = CATALOG_VERSION
        numeric["status"] = "PASS" if all(numeric[key] == 0 for key in invariant_keys) else "FAIL"
        return numeric
