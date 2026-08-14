from __future__ import annotations

import os
import uuid

import pytest
from psycopg import errors

from knowledge_engine.graph.repository import GraphRepository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 for rollback-only external-ingestion PostgreSQL tests",
)


def test_external_document_provenance_lifecycle_and_outer_rollback():
    repository = GraphRepository()
    run_id = str(uuid.uuid4())
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO external_acquisition_runs(run_id,status) VALUES (%s,'RUNNING')", (run_id,)
        )
        candidate_id = connection.execute(
            """INSERT INTO external_acquisition_candidates(
                 run_id,target_key,reference_kind,normalized_identifier,discovery_status,
                 source_domain,source_authority,identity_match_status,acquisition_status,
                 verification_status,source_adapter,sha256,local_acquired_path)
               VALUES (%s,%s,'ACT','TEST_ACT_2026','DISCOVERED','example.gov','OFFICIAL_PRIMARY',
                 'MATCH_EXACT','READY_FOR_INGESTION','AUTO_VERIFIED','test',%s,%s) RETURNING id""",
            (run_id, f"ACT|TEST_{run_id}", digest, f"data/acquisition/quarantine/{run_id}.pdf"),
        ).fetchone()["id"]
        document_id = connection.execute(
            """INSERT INTO documents(
                 sha256,file_path,file_name,department,page_count,extraction_method,raw_text,normalized_text,
                 source_origin,document_kind,canonical_identity,document_year)
               VALUES (%s,%s,%s,'example.gov',1,'digital','x','x','EXTERNAL_OFFICIAL','ACT',%s,2026)
               RETURNING id""",
            (digest, f"{run_id}.pdf", f"{run_id}.pdf", f"TEST_ACT_{run_id}"),
        ).fetchone()["id"]
        connection.execute(
            """INSERT INTO external_document_provenance(
                 document_id,external_candidate_id,original_acquisition_path,acquired_sha256)
               VALUES (%s,%s,%s,%s)""",
            (document_id, candidate_id, f"data/acquisition/quarantine/{run_id}.pdf", digest),
        )
        connection.execute(
            """INSERT INTO external_acquisition_lifecycle_events(external_candidate_id,lifecycle_status)
               VALUES (%s,'READY_FOR_INGESTION'),(%s,'INGESTED')""", (candidate_id, candidate_id)
        )
        with pytest.raises(errors.UniqueViolation), connection.transaction():
            connection.execute(
                """INSERT INTO external_document_provenance(
                     document_id,external_candidate_id,original_acquisition_path,acquired_sha256)
                   VALUES (%s,%s,'duplicate.pdf',%s)""", (document_id, candidate_id, digest),
            )
        assert connection.execute(
            "SELECT count(*) n FROM external_acquisition_lifecycle_events WHERE external_candidate_id=%s", (candidate_id,)
        ).fetchone()["n"] == 2
        connection.rollback()
    with repository.connect() as verification:
        assert verification.execute("SELECT count(*) n FROM documents WHERE sha256=%s", (digest,)).fetchone()["n"] == 0
