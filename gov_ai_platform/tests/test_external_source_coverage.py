from __future__ import annotations

from knowledge_engine.external_acquisition.coverage import YES, coverage_row, infer_issuer_from_evidence
from knowledge_engine.external_acquisition.models import ReferenceIdentity
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY


def source(source_id):
    return next(item for item in SOURCE_REGISTRY if item.source_id == source_id)


def test_operational_semantics_do_not_equate_kind_support_with_queryability():
    reference = ReferenceIdentity(
        target_key="GOVERNMENT_RESOLUTION|abc-2021/1",
        reference_kind="GOVERNMENT_RESOLUTION",
        raw_identifier="ABC-2021/1",
        normalized_identifier="abc-2021/1",
    )
    row = coverage_row(source("maharashtra_gr"), reference, quality_state="READY_FOR_DISCOVERY")
    assert row["ADAPTER_KIND_SUPPORT"] == YES
    assert row["ADAPTER_SEARCH_EXECUTABLE"] == "NO"
    assert row["TARGET_QUERYABLE"] == "NO"


def test_department_queryability_requires_matching_explicit_issuer():
    reference = ReferenceIdentity(
        target_key="GOVERNMENT_RESOLUTION|अभियान-2021/1",
        reference_kind="GOVERNMENT_RESOLUTION",
        raw_identifier="अभियान-2021/1",
        normalized_identifier="अभियान-2021/1",
        issuer="environment department",
    )
    row = coverage_row(source("maharashtra_environment"), reference, quality_state="READY_FOR_DISCOVERY")
    assert row["ADAPTER_ISSUER_SUPPORT"] == YES
    assert row["TARGET_QUERYABLE"] == YES


def test_law_and_judiciary_is_a_reusable_legal_source_family():
    reference = ReferenceIdentity(
        target_key="ACT|MAHARASHTRA_SLUM_AREAS_ACT_1971",
        reference_kind="ACT",
        raw_identifier="The Maharashtra Slum Areas Act, 1971",
        normalized_identifier="MAHARASHTRA_SLUM_AREAS_ACT_1971",
    )
    row = coverage_row(source("maharashtra_law_judiciary"), reference, quality_state="READY_FOR_DISCOVERY")
    assert row["TARGET_QUERYABLE"] == YES
    assert row["SOURCE_COVERAGE_CONFIRMED"] == YES


def test_state_law_source_does_not_claim_central_act_jurisdiction():
    reference = ReferenceIdentity(
        target_key="ACT|NATIONAL_GREEN_TRIBUNAL_ACT_2010",
        reference_kind="ACT",
        raw_identifier="National Green Tribunal Act, 2010",
        normalized_identifier="NATIONAL_GREEN_TRIBUNAL_ACT_2010",
    )
    row = coverage_row(source("maharashtra_law_judiciary"), reference, quality_state="READY_FOR_DISCOVERY")
    assert row["ADAPTER_JURISDICTION_SUPPORT"] == "NO"
    assert row["TARGET_QUERYABLE"] == "NO"


def test_issuer_inference_uses_explicit_evidence_not_citing_department():
    raw, normalized, observations = infer_issuer_from_evidence([
        {"raw_evidence_text": "पर्यावरण व वातावरणीय बदल विभाग शासन निर्णय क्र. अभियान-2021/प्र.क्र.1"},
        {"raw_evidence_text": "पर्यावरण व वातावरणीय बदल विभाग शासन निर्णय क्र. अभियान-2021/प्र.क्र.1"},
    ])
    assert raw == "पर्यावरण व वातावरणीय बदल विभाग"
    assert normalized == "environment department"
    assert observations == 2
