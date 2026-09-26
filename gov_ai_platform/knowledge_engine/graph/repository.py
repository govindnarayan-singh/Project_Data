from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from psycopg.rows import dict_row

from knowledge_engine.settings import PROJECT_ROOT, settings

from .models import ReferenceCandidate, Resolution
from .normalization import normalize_department_alias


MIGRATION_PATH = PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "001_kg_v1.sql"
AUTO_POLICY_MIGRATION_PATH = PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "002_kg_auto_review_policy.sql"


class GraphRepository:
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

    def apply_auto_policy_migration(self) -> None:
        sql = AUTO_POLICY_MIGRATION_PATH.read_text(encoding="utf-8")
        with self.connect() as connection:
            with connection.transaction():
                connection.execute(sql)

    def apply_auto_promotions(self, decisions) -> dict[str, int]:
        """Persist approved machine relations with candidate provenance only."""
        approved = [item for item in decisions if item.approved]
        inserted_relations = 0
        inserted_evidence = 0
        with self.connect() as connection:
            with connection.transaction():
                for item in approved:
                    if not all((item.candidate_id, item.source_document_id, item.target_document_id, item.relation_type)):
                        raise ValueError("An approved promotion is missing relation provenance")
                    row = connection.execute(
                        """INSERT INTO kg_relations(
                               source_document_id,target_document_id,relation_type,
                               validation_status,created_from_candidate_id)
                           VALUES (%s,%s,%s,'AUTO_VERIFIED',%s)
                           ON CONFLICT DO NOTHING RETURNING id""",
                        (item.source_document_id, item.target_document_id, item.relation_type, item.candidate_id),
                    ).fetchone()
                    if row:
                        relation_id = int(row["id"])
                        inserted_relations += 1
                    else:
                        existing = connection.execute(
                            """SELECT id FROM kg_relations
                               WHERE source_document_id=%s AND target_document_id=%s AND relation_type=%s""",
                            (item.source_document_id, item.target_document_id, item.relation_type),
                        ).fetchone()
                        if existing is None:
                            raise RuntimeError("Promotion conflict did not resolve to an existing relation")
                        relation_id = int(existing["id"])
                    evidence = connection.execute(
                        """INSERT INTO kg_relation_evidence(relation_id,candidate_id)
                           VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                        (relation_id, item.candidate_id),
                    ).fetchone()
                    inserted_evidence += int(evidence is not None)
        return {
            "approved_decisions": len(approved),
            "inserted_relations": inserted_relations,
            "inserted_evidence_links": inserted_evidence,
            "human_confirmed_created": 0,
        }

    def load_corpus(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self.connect() as connection:
            documents = [dict(row) for row in connection.execute(
                """SELECT id,file_name,file_path,department,document_type,gr_number,gr_date,subject,
                          source_origin,document_kind,canonical_identity,document_year,
                          page_count,digital_pages,ocr_pages,extraction_method
                     FROM documents ORDER BY id"""
            )]
            pages = [dict(row) for row in connection.execute(
                """SELECT document_id,page_number,extraction_method,quality_score,text
                     FROM pages ORDER BY document_id,page_number"""
            )]
        return documents, pages

    def start_run(self, run_id: str, *, parser_version: str, normalizer_version: str, resolver_version: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO kg_extraction_runs(run_id,parser_version,normalizer_version,resolver_version,status)
                   VALUES (%s,%s,%s,%s,'RUNNING')""",
                (run_id, parser_version, normalizer_version, resolver_version),
            )
            connection.commit()

    def finish_run(self, run_id: str, *, status: str, documents: int, pages: int, candidates: int, resolved: int, edges: int, notes: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE kg_extraction_runs SET finished_at=now(),status=%s,documents_processed=%s,
                          pages_processed=%s,candidate_count=%s,resolved_count=%s,edge_count=%s,notes=%s
                    WHERE run_id=%s""",
                (status, documents, pages, candidates, resolved, edges, notes, run_id),
            )
            connection.commit()

    def seed_departments(self, documents: Iterable[dict[str, Any]], connection) -> dict[str, int]:
        for name in sorted({str(row["department"]) for row in documents}):
            normalized = normalize_department_alias(name)
            row = connection.execute(
                """INSERT INTO kg_departments(canonical_name,normalized_name) VALUES (%s,%s)
                   ON CONFLICT (normalized_name) DO UPDATE SET canonical_name=EXCLUDED.canonical_name,updated_at=now()
                   RETURNING id""", (name, normalized),
            ).fetchone()
            department_id = int(row["id"])
            connection.execute(
                """INSERT INTO kg_department_aliases(department_id,raw_alias,normalized_alias)
                   VALUES (%s,%s,%s) ON CONFLICT (normalized_alias) DO NOTHING""",
                (department_id, name, normalized),
            )
        return {
            str(row["normalized_name"]): int(row["id"])
            for row in connection.execute("SELECT id,normalized_name FROM kg_departments")
        }

    def persist_run(
        self,
        run_id: str,
        documents: list[dict[str, Any]],
        items: list[tuple[ReferenceCandidate, Resolution]],
    ) -> dict[str, int]:
        with self.connect() as connection:
            with connection.transaction():
                department_ids = self.seed_departments(documents, connection)
                candidate_ids: dict[str, int] = {}
                inserted = 0
                updated = 0
                for candidate, resolution in items:
                    row = connection.execute(
                        """INSERT INTO kg_reference_candidates(
                               source_document_id,source_page,reference_kind,raw_evidence_text,
                               raw_document_number,normalized_document_number,raw_reference_date,
                               normalized_reference_date,date_parse_status,raw_department,normalized_department,
                               pattern_id,parser_version,parser_status,parser_notes,relation_signal,
                               candidate_relation_type,resolution_status,resolved_target_document_id,
                               resolution_method,resolution_evidence,resolution_candidate_count,confidence_category,
                               validation_status,blocked_by_page_quality,blocked_by_target_metadata,
                               evidence_hash,fingerprint,first_run_id,last_run_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (fingerprint) DO UPDATE SET
                               raw_evidence_text=EXCLUDED.raw_evidence_text,
                               raw_document_number=EXCLUDED.raw_document_number,
                               normalized_document_number=EXCLUDED.normalized_document_number,
                               raw_reference_date=EXCLUDED.raw_reference_date,
                               normalized_reference_date=EXCLUDED.normalized_reference_date,
                               date_parse_status=EXCLUDED.date_parse_status,
                               raw_department=EXCLUDED.raw_department,
                               normalized_department=EXCLUDED.normalized_department,
                               pattern_id=EXCLUDED.pattern_id,
                               parser_version=EXCLUDED.parser_version,parser_status=EXCLUDED.parser_status,
                               parser_notes=EXCLUDED.parser_notes,relation_signal=EXCLUDED.relation_signal,
                               candidate_relation_type=EXCLUDED.candidate_relation_type,
                               resolution_status=EXCLUDED.resolution_status,
                               resolved_target_document_id=EXCLUDED.resolved_target_document_id,
                               resolution_method=EXCLUDED.resolution_method,resolution_evidence=EXCLUDED.resolution_evidence,
                               resolution_candidate_count=EXCLUDED.resolution_candidate_count,
                               confidence_category=EXCLUDED.confidence_category,
                               validation_status=CASE
                                   WHEN kg_reference_candidates.validation_status IN ('HUMAN_CONFIRMED','REJECTED')
                                   THEN kg_reference_candidates.validation_status ELSE EXCLUDED.validation_status END,
                               blocked_by_page_quality=EXCLUDED.blocked_by_page_quality,
                               blocked_by_target_metadata=EXCLUDED.blocked_by_target_metadata,
                               last_run_id=EXCLUDED.last_run_id,updated_at=now()
                           RETURNING id,(xmax=0) AS inserted""",
                        (
                            candidate.source_document_id, candidate.source_page, candidate.reference_kind.value,
                            candidate.raw_evidence_text, candidate.raw_document_number, candidate.normalized_document_number,
                            candidate.raw_reference_date, candidate.normalized_reference_date, candidate.date_parse_status,
                            candidate.raw_department, candidate.normalized_department, candidate.pattern_id,
                            candidate.parser_version, candidate.parser_status.value, candidate.parser_notes,
                            candidate.relation_signal, candidate.candidate_relation_type, resolution.status.value,
                            resolution.target_document_id, resolution.method, resolution.evidence,
                            resolution.candidate_count, resolution.confidence_category, resolution.validation_status.value,
                            resolution.blocked_by_page_quality, resolution.blocked_by_target_metadata,
                            candidate.evidence_hash, candidate.fingerprint, run_id, run_id,
                        ),
                    ).fetchone()
                    candidate_ids[candidate.fingerprint] = int(row["id"])
                    inserted += int(bool(row["inserted"]))
                    updated += int(not bool(row["inserted"]))

                reference_edges = 0
                evidence_links = 0
                for candidate, resolution in items:
                    if resolution.validation_status.value != "AUTO_VERIFIED" or not resolution.target_document_id:
                        continue
                    candidate_id = candidate_ids[candidate.fingerprint]
                    row = connection.execute(
                        """INSERT INTO kg_relations(source_document_id,target_document_id,relation_type,validation_status,created_from_candidate_id)
                           VALUES (%s,%s,'REFERENCES','AUTO_VERIFIED',%s)
                           ON CONFLICT DO NOTHING RETURNING id""",
                        (candidate.source_document_id, resolution.target_document_id, candidate_id),
                    ).fetchone()
                    if row:
                        relation_id = int(row["id"])
                        reference_edges += 1
                    else:
                        relation_id = int(connection.execute(
                            """SELECT id FROM kg_relations WHERE source_document_id=%s AND target_document_id=%s AND relation_type='REFERENCES'""",
                            (candidate.source_document_id, resolution.target_document_id),
                        ).fetchone()["id"])
                    linked = connection.execute(
                        """INSERT INTO kg_relation_evidence(relation_id,candidate_id) VALUES (%s,%s)
                           ON CONFLICT DO NOTHING RETURNING id""", (relation_id, candidate_id),
                    ).fetchone()
                    evidence_links += int(linked is not None)

                issued_by_edges = 0
                for document in documents:
                    department_id = department_ids[normalize_department_alias(str(document["department"]))]
                    row = connection.execute(
                        """INSERT INTO kg_relations(source_document_id,target_department_id,relation_type,validation_status)
                           VALUES (%s,%s,'ISSUED_BY','AUTO_VERIFIED') ON CONFLICT DO NOTHING RETURNING id""",
                        (int(document["id"]), department_id),
                    ).fetchone()
                    issued_by_edges += int(row is not None)
        return {
            "inserted_candidates": inserted,
            "updated_candidates": updated,
            "reference_edges_inserted": reference_edges,
            "issued_by_edges_inserted": issued_by_edges,
            "evidence_links_inserted": evidence_links,
        }

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.id,d.file_name,d.file_path,d.department,d.document_type,d.gr_number,d.gr_date,d.subject,
                          d.page_count,count(DISTINCT c.id) AS chunk_count
                     FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                    WHERE d.id=%s GROUP BY d.id""", (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_outgoing_references(self, document_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.id AS relation_id,r.validation_status,d.id AS target_document_id,d.file_name AS target_file,
                          c.source_page,c.raw_evidence_text,c.raw_document_number,c.normalized_document_number,
                          c.resolution_status,c.reference_kind
                     FROM kg_relations r JOIN documents d ON d.id=r.target_document_id
                     LEFT JOIN kg_reference_candidates c ON c.id=r.created_from_candidate_id
                    WHERE r.source_document_id=%s AND r.relation_type='REFERENCES' ORDER BY c.source_page,r.id""", (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_incoming_references(self, document_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.id AS relation_id,r.validation_status,d.id AS source_document_id,d.file_name AS source_file,
                          c.source_page,c.raw_evidence_text,c.raw_document_number,c.normalized_document_number,
                          c.resolution_status,c.reference_kind
                     FROM kg_relations r JOIN documents d ON d.id=r.source_document_id
                     LEFT JOIN kg_reference_candidates c ON c.id=r.created_from_candidate_id
                    WHERE r.target_document_id=%s AND r.relation_type='REFERENCES' ORDER BY d.file_name,c.source_page""", (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_unresolved_references(self, document_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id AS candidate_id,source_page,reference_kind,raw_evidence_text,raw_document_number,
                          normalized_document_number,raw_reference_date,normalized_reference_date,resolution_status,
                          resolution_method,validation_status,candidate_relation_type,relation_signal
                     FROM kg_reference_candidates WHERE source_document_id=%s
                      AND resolution_status IN ('AMBIGUOUS','NOT_IN_CORPUS','UNPARSEABLE','NEEDS_REVIEW')
                    ORDER BY source_page,id""", (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_issued_by(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.id AS relation_id,r.validation_status,d.id AS department_id,d.canonical_name
                     FROM kg_relations r JOIN kg_departments d ON d.id=r.target_department_id
                    WHERE r.source_document_id=%s AND r.relation_type='ISSUED_BY'""", (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_relation_evidence(self, relation_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.id AS candidate_id,c.source_document_id,c.source_page,c.raw_evidence_text,
                          c.raw_document_number,c.normalized_document_number,c.resolution_status,c.validation_status
                     FROM kg_relation_evidence e JOIN kg_reference_candidates c ON c.id=e.candidate_id
                    WHERE e.relation_id=%s ORDER BY c.source_page,c.id""", (relation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_relation_signals(self, document_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id AS candidate_id,source_page,candidate_relation_type,relation_signal,raw_evidence_text,
                          resolution_status,validation_status FROM kg_reference_candidates
                    WHERE source_document_id=%s AND candidate_relation_type<>'' ORDER BY source_page,id""", (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_graph_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                    (SELECT count(DISTINCT source_document_id) FROM kg_relations) documents_represented,
                    (SELECT count(*) FROM kg_departments) departments_represented,
                    (SELECT count(*) FROM kg_reference_candidates) reference_candidates,
                    (SELECT count(*) FROM kg_reference_candidates WHERE resolution_status IN ('RESOLVED_EXACT','RESOLVED_STRONG')) resolved_candidates,
                    (SELECT count(*) FROM kg_reference_candidates WHERE validation_status='AUTO_VERIFIED') auto_verified_candidates,
                    (SELECT count(*) FROM kg_reference_candidates WHERE resolution_status='NOT_IN_CORPUS') unresolved_candidates,
                    (SELECT count(*) FROM kg_reference_candidates WHERE resolution_status='AMBIGUOUS') ambiguous_candidates,
                    (SELECT count(*) FROM kg_reference_candidates WHERE resolution_status='UNPARSEABLE') unparseable_candidates,
                    (SELECT count(*) FROM kg_relations WHERE relation_type='REFERENCES') references_edges,
                    (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by_edges"""
            ).fetchone()
        return {key: int(value) for key, value in row.items()}

    def all_candidates(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.*,s.file_name AS source_file,s.department AS source_department,t.file_name AS target_file,
                          t.department AS target_department,t.source_origin AS target_source_origin,
                          ep.external_candidate_id AS target_external_candidate_id,
                          p.extraction_method AS source_extraction_method,p.quality_score AS source_quality_score
                     FROM kg_reference_candidates c JOIN documents s ON s.id=c.source_document_id
                     JOIN pages p ON p.document_id=c.source_document_id AND p.page_number=c.source_page
                     LEFT JOIN documents t ON t.id=c.resolved_target_document_id
                     LEFT JOIN external_document_provenance ep ON ep.document_id=t.id ORDER BY c.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def targeted_unresolved_candidates(self, normalized_identifier: str) -> list[dict[str, Any]]:
        """Load only unresolved evidence that can match one newly admitted identity."""
        if not normalized_identifier:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.*,s.file_name AS source_file,s.department AS source_department
                     FROM kg_reference_candidates c
                     JOIN documents s ON s.id=c.source_document_id
                    WHERE c.normalized_document_number=%s
                      AND c.resolution_status IN ('AMBIGUOUS','NOT_IN_CORPUS','UNPARSEABLE','NEEDS_REVIEW')
                      AND c.validation_status NOT IN ('HUMAN_CONFIRMED','REJECTED')
                    ORDER BY c.id""",
                (normalized_identifier,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_edges(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.*,s.file_name AS source_file,t.file_name AS target_file,d.canonical_name AS target_department
                     FROM kg_relations r JOIN documents s ON s.id=r.source_document_id
                     LEFT JOIN documents t ON t.id=r.target_document_id
                     LEFT JOIN kg_departments d ON d.id=r.target_department_id ORDER BY r.id"""
            ).fetchall()
        return [dict(row) for row in rows]
