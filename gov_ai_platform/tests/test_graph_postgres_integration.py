from __future__ import annotations

import os
import uuid

import pytest
from psycopg import errors

from knowledge_engine.graph.repository import GraphRepository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 for rollback-only PostgreSQL KG tests",
)


def _insert_run(connection, run_id):
    connection.execute(
        """INSERT INTO kg_extraction_runs(run_id,parser_version,normalizer_version,resolver_version,status)
           VALUES (%s,'test-parser','test-normalizer','test-resolver','RUNNING')""", (run_id,),
    )


def _insert_candidate(connection, run_id, fingerprint, *, resolved=False):
    return connection.execute(
        """INSERT INTO kg_reference_candidates(
            source_document_id,source_page,reference_kind,raw_evidence_text,raw_document_number,
            normalized_document_number,raw_reference_date,date_parse_status,pattern_id,parser_version,
            parser_status,resolution_status,resolved_target_document_id,resolution_method,resolution_evidence,
            confidence_category,validation_status,evidence_hash,fingerprint,first_run_id,last_run_id)
        VALUES (4,1,'GOVERNMENT_RESOLUTION','integration evidence','ABC/2020/1','abc/2020/1','',
            'MISSING','TEST','test-parser','VALID',%s,%s,%s,'integration test','HIGH',%s,%s,%s,%s,%s)
        RETURNING id""",
        (
            "RESOLVED_EXACT" if resolved else "NOT_IN_CORPUS", 5 if resolved else None,
            "EXACT_NORMALIZED_NUMBER" if resolved else "NO_NORMALIZED_NUMBER_MATCH",
            "AUTO_VERIFIED" if resolved else "UNRESOLVED", "a" * 64, fingerprint, run_id, run_id,
        ),
    ).fetchone()["id"]


def test_candidate_edge_evidence_constraints_and_outer_rollback():
    repository = GraphRepository()
    run_id = str(uuid.uuid4())
    with repository.connect() as connection:
        _insert_run(connection, run_id)
        unresolved_id = _insert_candidate(connection, run_id, "1" * 64)
        resolved_id = _insert_candidate(connection, run_id, "2" * 64, resolved=True)
        relation_id = connection.execute(
            """INSERT INTO kg_relations(source_document_id,target_document_id,relation_type,validation_status,created_from_candidate_id)
               VALUES (4,5,'REFERENCES','AUTO_VERIFIED',%s) RETURNING id""", (resolved_id,),
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO kg_relation_evidence(relation_id,candidate_id) VALUES (%s,%s)",
            (relation_id, resolved_id),
        )
        with pytest.raises(errors.UniqueViolation), connection.transaction():
            _insert_candidate(connection, run_id, "1" * 64)
        with pytest.raises(errors.ForeignKeyViolation), connection.transaction():
            connection.execute("UPDATE kg_reference_candidates SET source_document_id=999999 WHERE id=%s", (unresolved_id,))
        assert connection.execute("SELECT count(*) count FROM kg_relation_evidence WHERE relation_id=%s", (relation_id,)).fetchone()["count"] == 1
        connection.rollback()
    with repository.connect() as verification:
        assert verification.execute("SELECT count(*) count FROM kg_extraction_runs WHERE run_id=%s", (run_id,)).fetchone()["count"] == 0
