from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from knowledge_engine.retrieval import graph_eval
from knowledge_engine.retrieval.graph_aware import (
    ALLOWED_VALIDATION_STATES,
    GraphAwareRetrievalService,
    GraphDirection,
    PostgresGraphExpansionRepository,
)
from knowledge_engine.settings import settings


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, show_progress_bar=False):
        self.calls.append((texts, show_progress_bar))
        return [[0.1, 0.2, 0.3]]


def dense_row(document_id: int, rank: int) -> dict:
    return {
        "document_id": document_id, "file_name": f"d{document_id}.pdf",
        "file_path": f"Dept/d{document_id}.pdf", "department": "Dept",
        "gr_number": f"GR-{document_id}", "gr_date": date(2024, 1, 1),
        "subject": f"Document {document_id}", "chunk_index": rank,
        "page_start": rank, "page_end": rank, "text": f"text {document_id}",
        "similarity": 1.0 - rank / 100,
    }


def edge(relation_id: int, source: int, target: int) -> dict:
    return {
        "relation_id": relation_id, "relation_type": "REFERENCES", "validation_status": "AUTO_VERIFIED",
        "source_document_id": source, "target_document_id": target,
        "source_file": f"d{source}.pdf", "target_file": f"d{target}.pdf",
        "source_file_path": f"Dept/d{source}.pdf", "target_file_path": f"Dept/d{target}.pdf",
        "source_department": "Dept", "target_department": "Dept",
        "source_gr_number": f"GR-{source}", "target_gr_number": f"GR-{target}",
        "source_subject": f"Document {source}", "target_subject": f"Document {target}",
        "candidate_id": 1000 + relation_id, "source_page": relation_id,
        "raw_evidence_text": f"evidence {relation_id}",
    }


class FakeDenseRepository:
    def __init__(self):
        self.rows = [dense_row(value, value) for value in range(1, 11)]
        self.calls = []

    def dense_search(self, vector, top_k=10, department=None):
        self.calls.append((vector, top_k, department))
        return self.rows[:top_k]


class FakeGraphRepository:
    def __init__(self):
        self.edges = [edge(1, 1, 12), edge(2, 1, 11), edge(3, 13, 1), edge(4, 2, 12), edge(5, 1, 2)]
        self.direction_calls = []
        self.page_calls = []

    def validated_reference_neighbors(self, seed_document_ids, direction):
        self.direction_calls.append((seed_document_ids, direction))
        return self.edges

    def dense_search_document(self, vector, document_id, top_k=1):
        self.page_calls.append((document_id, top_k))
        return [dense_row(document_id, 7)]


def service():
    return GraphAwareRetrievalService(FakeEmbedder(), FakeDenseRepository(), FakeGraphRepository())


def test_dense_baseline_module_is_unchanged_by_graph_service() -> None:
    dense_source = Path("knowledge_engine/retrieval/dense.py").read_text(encoding="utf-8")
    assert "graph" not in dense_source.casefold()
    assert "0.7" not in dense_source


def test_only_accepted_relation_states_are_allowed() -> None:
    assert ALLOWED_VALIDATION_STATES == {"AUTO_VERIFIED", "HUMAN_CONFIRMED"}
    source = Path(PostgresGraphExpansionRepository.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    assert "r.relation_type='REFERENCES'" in source
    assert "'AUTO_VERIFIED','HUMAN_CONFIRMED'" in source


@pytest.mark.parametrize(
    ("direction", "addition_ids"),
    [
        ("outgoing", [11, 12]),
        ("incoming", [13]),
        ("bidirectional", [11, 12, 13]),
    ],
)
def test_directional_one_hop_expansion(direction: str, addition_ids: list[int]) -> None:
    result = service().search("query", seed_top_k=1, direction=direction)
    assert [item.document_id for item in result.graph_additions] == addition_ids
    assert all(path.graph_distance == 1 for item in result.graph_additions for path in item.paths)
    assert {path.direction for item in result.graph_additions for path in item.paths} <= {direction.upper()} if direction != "bidirectional" else {"OUTGOING", "INCOMING"}


@pytest.mark.parametrize("seed_top_k", [1, 3, 5])
def test_seed_depth_is_explicit_and_bounded(seed_top_k: int) -> None:
    graph = FakeGraphRepository()
    result = GraphAwareRetrievalService(FakeEmbedder(), FakeDenseRepository(), graph).search(
        "query", seed_top_k=seed_top_k, direction="outgoing"
    )
    assert result.seed_top_k == seed_top_k
    assert len(graph.direction_calls[0][0]) == seed_top_k


def test_duplicate_dense_neighbor_is_corroboration_not_duplicate() -> None:
    result = service().search("query", seed_top_k=1, direction="outgoing")
    assert [item.document_id for item in result.corroborations] == [2]
    assert result.corroborations[0].duplicate_state == "ALREADY_IN_DENSE"
    assert 2 not in [item.document_id for item in result.graph_additions]


def test_multiple_paths_are_retained_on_one_graph_document() -> None:
    result = service().search("query", seed_top_k=3, direction="outgoing")
    document = next(item for item in result.graph_additions if item.document_id == 12)
    assert [path.relation_id for path in document.paths] == [1, 4]


def test_provenance_and_relation_evidence_are_complete() -> None:
    result = service().search("query", seed_top_k=1, direction="outgoing")
    path = result.graph_additions[0].paths[0]
    assert path.relation_type == "REFERENCES"
    assert path.validation_state == "AUTO_VERIFIED"
    assert path.source_seed_document_id == 1
    assert path.seed_dense_rank == 1
    assert path.seed_dense_similarity > 0
    assert path.relation_id and path.candidate_id
    assert path.source_evidence_page and path.raw_edge_evidence


def test_graph_candidate_page_retrieval_is_restricted_to_added_document() -> None:
    graph = FakeGraphRepository()
    result = GraphAwareRetrievalService(FakeEmbedder(), FakeDenseRepository(), graph).search(
        "query", seed_top_k=1, direction="outgoing"
    )
    assert graph.page_calls == [(11, 1), (12, 1)]
    assert all(item.query_relevant_page_similarity is not None for item in result.graph_additions)


def test_deterministic_order_is_seed_relation_document_then_relation() -> None:
    first = service().search("query", seed_top_k=3, direction="bidirectional")
    second = service().search("query", seed_top_k=3, direction="bidirectional")
    assert [item.document_id for item in first.graph_additions] == [item.document_id for item in second.graph_additions]
    assert [item.document_id for item in first.graph_additions] == [11, 12, 13]


def test_fanout_limits_are_visible_and_not_silent() -> None:
    result = service().search(
        "query", seed_top_k=1, direction="outgoing",
        max_graph_additions_per_seed=1, max_graph_additions_total=1,
    )
    assert len(result.graph_additions) == 1
    assert result.limits_reached


def test_no_score_fusion_field_or_formula_exists() -> None:
    source = Path("knowledge_engine/retrieval/graph_aware.py").read_text(encoding="utf-8")
    assert "dense_similarity *" not in source
    assert "graph_score" not in source
    assert all(not hasattr(item, "score") for item in service().search("query").graph_additions)


def test_graph_recovery_and_precision_formulas_use_explicit_denominators() -> None:
    rows = [
        {"graph_recovery_eligible": True, "graph_recovered": True, "graph_additions": 2, "useful_graph_additions": 1,
         "provenance_records": 2, "complete_provenance_records": 2, "unique_neighbors": 2, "corroborations": 0,
         "wrong_direction_paths": 0, "raw_neighbors": 2, "limits_reached": "", "total_latency_ms": 10,
         "graph_expansion_ms": 2, "restricted_page_ms": 3},
        {"graph_recovery_eligible": True, "graph_recovered": False, "graph_additions": 1, "useful_graph_additions": 0,
         "provenance_records": 1, "complete_provenance_records": 1, "unique_neighbors": 1, "corroborations": 0,
         "wrong_direction_paths": 1, "raw_neighbors": 1, "limits_reached": "", "total_latency_ms": 20,
         "graph_expansion_ms": 4, "restricted_page_ms": 6},
    ]
    metrics = graph_eval._variant_metrics(rows)
    assert metrics["graph_recovery_rate"] == {"numerator": 1, "denominator": 2, "percentage": 0.5}
    assert metrics["graph_expansion_precision"] == {"numerator": 1, "denominator": 3, "percentage": 1 / 3}


def test_graph_service_has_no_database_mutation_path() -> None:
    source = Path("knowledge_engine/retrieval/graph_aware.py").read_text(encoding="utf-8").casefold()
    for statement in ("insert into", "update ", "delete from", "create table", "drop table"):
        assert statement not in source


def test_experimental_mode_keeps_dense_default() -> None:
    assert settings.retrieval_mode == "dense"
    assert "RETRIEVAL_MODE=dense" in Path(".env.example").read_text(encoding="utf-8")


def test_runtime_contract_has_no_podman_or_backup_execution() -> None:
    bodies = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in ("knowledge_engine/retrieval/graph_aware.py", "knowledge_engine/retrieval/graph_eval.py", "cli/graph_retrieval_eval.py")
    ).casefold()
    assert "podman " not in bodies
    assert "pg_dump" not in bodies
    assert "backup_database" not in bodies


def test_graph_benchmark_is_source_evidence_labelled() -> None:
    assert graph_eval.GRAPH_BENCHMARK_FIELDS[-2:] == ("label_source", "independent_human_ground_truth")


def test_invalid_depth_or_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        service().search("query", seed_top_k=2)
    with pytest.raises(ValueError):
        service().search("query", max_graph_additions_total=0)

