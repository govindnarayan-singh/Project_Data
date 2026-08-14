from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from knowledge_engine.settings import PROJECT_ROOT, settings

from .models import ReferenceIdentity, SourceCandidate


MIGRATION_PATH = PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "004_external_acquisition_v1.sql"


class ExternalAcquisitionRepository:
    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def apply_migration(self) -> None:
        with self.connect() as connection:
            with connection.transaction():
                connection.execute(MIGRATION_PATH.read_text(encoding="utf-8"))

    def start_run(self, run_id: str, target_count: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO external_acquisition_runs(run_id,status,targets_selected)
                   VALUES (%s,'RUNNING',%s)
                   ON CONFLICT (run_id) DO UPDATE SET status='RUNNING',targets_selected=EXCLUDED.targets_selected""",
                (run_id, target_count),
            )
            connection.commit()

    def finish_run(self, run_id: str, *, status: str, discovered: int, notes: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE external_acquisition_runs SET finished_at=now(),status=%s,
                          candidates_discovered=%s,notes=%s WHERE run_id=%s""",
                (status, discovered, notes[:2000], run_id),
            )
            connection.commit()

    def upsert(self, run_id: str, reference: ReferenceIdentity, candidate: SourceCandidate) -> tuple[int, bool]:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO external_acquisition_candidates(
                       run_id,target_key,reference_kind,raw_reference_identifier,normalized_identifier,
                       reference_date,issuer,discovery_query,discovery_status,source_url,final_url,
                       source_domain,source_authority,download_url,discovered_title,discovered_identifier,
                       discovered_date,identity_match_status,identity_evidence,acquisition_status,
                       verification_status,source_adapter,raw_source_evidence,priority_tier,priority_score,discovered_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           CASE WHEN %s='DISCOVERED' THEN now() ELSE NULL END)
                   ON CONFLICT (target_key,source_adapter,source_url,discovered_identifier) DO UPDATE SET
                       run_id=EXCLUDED.run_id,discovery_query=EXCLUDED.discovery_query,
                       discovery_status=EXCLUDED.discovery_status,final_url=EXCLUDED.final_url,
                       discovered_title=EXCLUDED.discovered_title,discovered_date=EXCLUDED.discovered_date,
                       identity_match_status=EXCLUDED.identity_match_status,identity_evidence=EXCLUDED.identity_evidence,
                       acquisition_status=EXCLUDED.acquisition_status,verification_status=EXCLUDED.verification_status,
                       raw_source_evidence=EXCLUDED.raw_source_evidence,priority_tier=EXCLUDED.priority_tier,
                       priority_score=EXCLUDED.priority_score,updated_at=now()
                   RETURNING id,(xmax=0) AS inserted""",
                (
                    run_id, reference.target_key, reference.reference_kind, reference.raw_identifier,
                    reference.normalized_identifier, reference.reference_date, reference.issuer,
                    candidate.discovery_query, candidate.discovery_status, candidate.source_url,
                    candidate.final_url, candidate.source_domain, candidate.source_authority,
                    candidate.download_url, candidate.discovered_title, candidate.discovered_identifier,
                    candidate.discovered_date, candidate.identity_match_status, Jsonb(candidate.identity_evidence),
                    candidate.acquisition_status, candidate.verification_status, candidate.source_adapter,
                    Jsonb(candidate.raw_source_evidence), reference.priority_tier, reference.priority_score,
                    candidate.discovery_status,
                ),
            ).fetchone()
            connection.commit()
        return int(row["id"]), bool(row["inserted"])

    def rows(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if run_id:
                rows = connection.execute(
                    "SELECT * FROM external_acquisition_candidates WHERE run_id=%s ORDER BY priority_score DESC,id",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM external_acquisition_candidates ORDER BY priority_score DESC,id"
                ).fetchall()
        return [dict(row) for row in rows]

    def integrity(self) -> dict[str, int | str]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                  (SELECT count(*) FROM external_acquisition_candidates) catalogue_rows,
                  (SELECT coalesce(sum(n-1),0) FROM (
                     SELECT count(*) n FROM external_acquisition_candidates
                     GROUP BY target_key,source_adapter,source_url,discovered_identifier HAVING count(*)>1) x) duplicate_candidates,
                  (SELECT count(*) FROM external_acquisition_candidates WHERE source_authority='NON_AUTHORITATIVE'
                     AND verification_status='AUTO_VERIFIED') non_authoritative_auto_verified,
                  (SELECT count(*) FROM external_acquisition_candidates WHERE acquisition_status='READY_FOR_INGESTION'
                     AND (local_acquired_path='' OR sha256='' OR verification_status<>'AUTO_VERIFIED')) invalid_ready_rows,
                  (SELECT count(*) FROM documents d JOIN external_acquisition_candidates e ON d.file_path=e.local_acquired_path
                     WHERE e.local_acquired_path<>'') already_ingested_external_paths"""
            ).fetchone()
        result = {key: int(value) for key, value in row.items()}
        result["status"] = "PASS" if all(result[key] == 0 for key in (
            "duplicate_candidates", "non_authoritative_auto_verified", "invalid_ready_rows"
        )) else "FAIL"
        return result


__all__ = ["ExternalAcquisitionRepository"]
