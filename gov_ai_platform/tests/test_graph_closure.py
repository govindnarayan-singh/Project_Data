from knowledge_engine.graph.closure import build_closure_snapshot, target_identity


def candidate(candidate_id, status, *, number="A/1", kind="GOVERNMENT_RESOLUTION", target=None, local=None):
    return {
        "id": candidate_id,
        "source_document_id": candidate_id + 10,
        "source_page": 1,
        "reference_kind": kind,
        "normalized_document_number": number,
        "resolution_status": status,
        "resolved_target_document_id": target,
        "local_resolution_status": "RESOLVED_LOCAL_EXACT" if local else "",
        "resolved_target_catalog_id": local,
    }


def test_target_identity_is_exact_and_canonicalizes_circular_alias_only():
    assert target_identity(candidate(1, "NOT_IN_CORPUS", number="A/1", kind="CIRCULAR")) == (
        "GOVERNMENT_CIRCULAR",
        "A/1",
    )
    assert target_identity(candidate(1, "NOT_IN_CORPUS", number="A/2")) != target_identity(
        candidate(2, "NOT_IN_CORPUS", number="A/1")
    )


def test_mentions_and_targets_use_separate_denominators_and_dedupe_exact_targets():
    rows = [
        candidate(1, "RESOLVED_EXACT", target=20),
        candidate(2, "NOT_IN_CORPUS"),
        candidate(3, "NOT_IN_CORPUS", number="B/2", local=99),
        candidate(4, "SELF_REFERENCE_REJECTED", number="SELF/1"),
        candidate(5, "UNPARSEABLE", number=""),
    ]
    snapshot = build_closure_snapshot(rows, [], total_documents=20)["summary"]
    assert snapshot["reference_mention_closure"] == {
        "numerator": 1,
        "denominator": 3,
        "percentage": 1 / 3,
    }
    assert snapshot["referenced_target_closure"]["denominator"] == 2
    assert snapshot["local_availability_closure"]["numerator"] == 2
    assert snapshot["excluded_mentions"] == {"SELF_REFERENCE_REJECTED": 1, "UNPARSEABLE": 1}


def test_graph_coverage_excludes_issued_by_and_unvalidated_relations():
    relations = [
        {"source_document_id": 1, "target_document_id": 2, "relation_type": "REFERENCES", "validation_status": "AUTO_VERIFIED"},
        {"source_document_id": 3, "target_document_id": None, "relation_type": "ISSUED_BY", "validation_status": "AUTO_VERIFIED"},
        {"source_document_id": 4, "target_document_id": 5, "relation_type": "AMENDS", "validation_status": "CANDIDATE"},
    ]
    metric = build_closure_snapshot([], relations, total_documents=5)["summary"]["graph_document_coverage"]
    assert metric == {"numerator": 2, "denominator": 5, "percentage": 0.4}
