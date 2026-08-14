from __future__ import annotations

from datetime import date
from pathlib import Path

from knowledge_engine.external_acquisition.adapters import (
    DivyangSahayakSource,
    IndiaCodeSource,
    _OfficialIndexParser,
)
from knowledge_engine.external_acquisition.identity import (
    legal_identity_quality,
    legal_version_semantics,
    verify_identity,
)
from knowledge_engine.external_acquisition.models import AcquisitionStrategy, ReferenceIdentity
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY
from knowledge_engine.external_acquisition.v2_1 import _normalize_v21_closure_baseline


def reference(identifier: str, *, kind: str = "ACT", issuer: str = "", reference_date: date | None = None) -> ReferenceIdentity:
    return ReferenceIdentity(
        target_key=f"{kind}|{identifier}", reference_kind=kind,
        raw_identifier=identifier, normalized_identifier=identifier,
        issuer=issuer, reference_date=reference_date,
        strategy=AcquisitionStrategy.INDIA_ACT if kind == "ACT" else AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
    )


def test_legal_quality_rejects_footnotes_and_prose_fragments() -> None:
    for value in (
        "1_INS_BY_ACT_2023", "THIS_ACT", "MAHARASHTRA_ACT",
        "NOTE_THE_DATE_MENTIONED_IN_THE_BRACKET_INDICATES_THE_DATE_OF_COMMENCEMENT_OF_THE_ACT",
    ):
        assert legal_identity_quality(value)[0] is False
    assert legal_identity_quality("TRIBUNAL_ACT_2010") == (True, "STRUCTURED_LEGAL_TITLE_YEAR")


def test_title_suffix_plus_exact_year_is_deterministic_match() -> None:
    result = verify_identity(
        reference("TRIBUNAL_ACT_2010"),
        discovered_title="The National Green Tribunal Act, 2010",
    )
    assert result.status == "MATCH_EXACT"
    assert result.evidence["legal_suffix_match"] is True
    assert verify_identity(
        reference("TRIBUNAL_ACT_2010"),
        discovered_title="The National Green Tribunal Act, 2011",
    ).status == "MISMATCH"


def test_principal_amendment_and_consolidated_versions_stay_distinct() -> None:
    assert legal_version_semantics("NATIONAL_GREEN_TRIBUNAL_ACT_2010") == "PRINCIPAL_ACT_EXACT"
    assert legal_version_semantics("NATIONAL_GREEN_TRIBUNAL_AMENDMENT_ACT_2023") == "AMENDMENT_ACT_EXACT"
    assert legal_version_semantics("NATIONAL_GREEN_TRIBUNAL_ACT_2010_AS_AMENDED") == "CONSOLIDATED_VERSION_OF_TARGET"


def test_administrative_prefix_requires_exact_reference_date() -> None:
    target = reference(
        "इडीडी-2022/प्र.क्र.65/", kind="GOVERNMENT_RESOLUTION",
        issuer="Persons with Disabilities Welfare Department", reference_date=date(2024, 7, 16),
    )
    matched = verify_identity(
        target, discovered_title="Official GR",
        discovered_identifier="इडीडी-2022/प्र.क्र.65/दि.क.1",
        discovered_date=date(2024, 7, 16), discovered_issuer=target.issuer,
    )
    assert matched.status == "MATCH_EXACT"
    assert matched.evidence["administrative_prefix_match"] is True
    assert verify_identity(
        target, discovered_title="Official GR",
        discovered_identifier="इडीडी-2022/प्र.क्र.65/दि.क.1",
        discovered_date=date(2024, 7, 15), discovered_issuer=target.issuer,
    ).status == "MISMATCH"


def test_official_index_parser_retains_row_title_date_and_pdf() -> None:
    parser = _OfficialIndexParser()
    parser.feed("""
        <table><tr><td>Disability school grant policy</td><td>16-07-2024</td>
        <td><a href='/uploads/GR/PWD/1/28.pdf'>View</a></td></tr></table>
    """)
    assert parser.rows == [(
        "/uploads/GR/PWD/1/28.pdf", "Disability school grant policy 16-07-2024 View", "2024-07-16"
    )]


def test_divyang_adapter_scope_is_reusable_not_target_specific(tmp_path: Path) -> None:
    definition = next(source for source in SOURCE_REGISTRY if source.source_id == "divyang_sahayak")
    adapter = DivyangSahayakSource(definition, cache_dir=tmp_path)
    assert adapter.supports(reference(
        "इडीडी-2022/प्र.क्र.65/", kind="GOVERNMENT_RESOLUTION",
        issuer="Persons with Disabilities Welfare Department", reference_date=date(2024, 7, 16),
    ))
    assert not adapter.supports(reference(
        "अभियान-2021/प्र.क्र.57/तां.क.1", kind="GOVERNMENT_RESOLUTION",
        issuer="Environment Department", reference_date=date(2022, 2, 8),
    ))


def test_india_code_rejects_bad_target_before_network(tmp_path: Path) -> None:
    definition = next(source for source in SOURCE_REGISTRY if source.source_id == "india_code")
    adapter = IndiaCodeSource(definition, cache_dir=tmp_path)

    class NoNetwork:
        def fetch(self, *args, **kwargs):
            raise AssertionError("network must not be called for invalid legal identity")

    adapter.client = NoNetwork()  # type: ignore[assignment]
    rows = adapter.search(reference("1_INS_BY_ACT_2023"))
    assert rows[0].target_state == "UNPARSEABLE_REFERENCE"
    assert rows[0].raw_source_evidence["reason"] == "IDENTIFIER_EXTRACTION_FAILURE"


def test_v21_closure_baseline_includes_archive_already_promoted_by_v2() -> None:
    before = {"local_archived_availability_closure": {"numerator": 7}}
    after = {"local_archived_availability_closure": {"numerator": 12}}
    normalized = _normalize_v21_closure_baseline(before, after)
    assert normalized["local_archived_availability_closure"]["numerator"] == 12
    assert before["local_archived_availability_closure"]["numerator"] == 7
