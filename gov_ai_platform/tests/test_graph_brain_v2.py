from __future__ import annotations

import hashlib
import os

import pytest

from knowledge_engine.external_ingestion.v2_2 import (
    EXPECTED_SHA256, EXPECTED_TARGET, FROZEN_MANIFEST_SHA256, MANIFEST,
    V22ExternalIngestionService,
)
from knowledge_engine.graph.analytics import _components, _cycles, completion_priority
from knowledge_engine.graph.benchmark_v2 import build_benchmark, evaluate_benchmark
from knowledge_engine.graph.brain_v2 import integrity_report
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.traversal import GraphTraversalService, _depth


def test_v22_frozen_manifest_is_exactly_one_local_archive_row():
    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == FROZEN_MANIFEST_SHA256
    rows = V22ExternalIngestionService.rows(MANIFEST)
    assert len(rows) == 1
    assert rows[0]["KG_target_group"] == EXPECTED_TARGET
    assert rows[0]["SHA-256"] == EXPECTED_SHA256
    assert rows[0]["document_source_root"] == "EXTERNAL_OFFICIAL"
    assert rows[0]["archive_status"] == "EXTERNAL_OFFICIAL_ARCHIVED"


@pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
def test_traversal_accepts_only_safe_bounded_depths(value):
    assert _depth(value) == value


@pytest.mark.parametrize("value", [-1, 0, 6, 100])
def test_traversal_rejects_unbounded_depths(value):
    with pytest.raises(ValueError, match="between 1 and 5"):
        _depth(value)


def test_components_include_isolates_and_bridge_separation():
    mapping, components = _components({1, 2, 3, 4}, [
        {"source_document_id": 1, "target_document_id": 2},
        {"source_document_id": 3, "target_document_id": 4},
    ])
    assert len(components) == 2
    assert mapping[1] == mapping[2]
    assert mapping[1] != mapping[3]


def test_cycle_detection_is_directional_and_deduplicated():
    cycles = _cycles([
        {"id": 10, "source_document_id": 1, "target_document_id": 2},
        {"id": 11, "source_document_id": 2, "target_document_id": 1},
    ])
    assert len(cycles) == 1
    assert cycles[0]["length"] == 2


class PriorityRepository:
    def all_candidates(self):
        return [
            {"resolution_status": "NOT_IN_CORPUS", "reference_kind": "ACT",
             "normalized_document_number": "EXAMPLE_ACT_2020", "raw_document_number": "Example Act, 2020",
             "source_document_id": 1, "candidate_relation_type": "AMENDS", "blocked_by_page_quality": False,
             "normalized_reference_date": "2020-01-01", "normalized_department": "law",
             "resolved_target_catalog_id": None},
            {"resolution_status": "NOT_IN_CORPUS", "reference_kind": "ACT",
             "normalized_document_number": "EXAMPLE_ACT_2020", "raw_document_number": "Example Act 2020",
             "source_document_id": 3, "candidate_relation_type": "", "blocked_by_page_quality": False,
             "normalized_reference_date": "2020-01-01", "normalized_department": "law",
             "resolved_target_catalog_id": None},
        ]


def test_graph_completion_priority_rewards_cross_component_bridge_and_is_explainable():
    rows, bridges = completion_priority({1: 1, 3: 2}, PriorityRepository())
    assert rows[0]["component_bridge_score"] == 1
    assert rows[0]["official_source_adapter_availability"] is True
    assert "2 citing docs" in rows[0]["priority_explanation"]
    assert bridges[0]["normalized_target_identity"] == rows[0]["normalized_target_identity"]


pytestmark_live = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 for native PostgreSQL graph tests",
)


@pytestmark_live
def test_live_recursive_traversal_direction_provenance_benchmark_and_invariants():
    repository = GraphRepository(); service = GraphTraversalService(repository)
    with repository.connect() as connection:
        edge = connection.execute(
            "SELECT source_document_id,target_document_id FROM kg_relations WHERE relation_type='REFERENCES' ORDER BY id LIMIT 1"
        ).fetchone()
    assert edge is not None
    source, target = int(edge["source_document_id"]), int(edge["target_document_id"])
    direct = service.upstream(source, 1)
    assert any(row["end_document_id"] == target for row in direct.paths)
    assert all(hop["provenance_complete"] for row in direct.paths for hop in row["hops"])
    reverse = service.downstream(target, 1)
    assert any(row["end_document_id"] == source for row in reverse.paths)
    shortest = service.paths(source, target, 5)
    assert shortest["status"] == "PATH_FOUND" and shortest["path_length"] == 1
    benchmark = build_benchmark(repository)
    result = evaluate_benchmark(benchmark, repository)
    assert 30 <= len(benchmark) <= 50
    assert result["status"] == "PASS"
    assert result["provenance_completeness"]["accuracy"] == 1.0
    assert integrity_report(repository)["status"] == "PASS"
