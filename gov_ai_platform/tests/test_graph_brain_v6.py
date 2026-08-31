from __future__ import annotations

from datetime import date
from pathlib import Path

from django.test import SimpleTestCase

from knowledge_engine.external_acquisition.adapters import IndiaCodeSource, MaharashtraS3WaaSDocumentSource
from knowledge_engine.external_acquisition.identity import expected_identifier
from knowledge_engine.external_acquisition.models import (
    AcquisitionStrategy,
    ReferenceIdentity,
)
from knowledge_engine.external_acquisition.queries import generate_queries
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY, validate_registry


def _reference(identity: str) -> ReferenceIdentity:
    return ReferenceIdentity(
        target_key=f"ACT|{identity}",
        reference_kind="ACT",
        raw_identifier=identity.replace("_", " "),
        normalized_identifier=identity,
        reference_date=date(int(identity.rsplit("_", 1)[-1]), 1, 1),
        strategy=AcquisitionStrategy.INDIA_ACT,
    )


class KgBrainV6Tests(SimpleTestCase):
    def test_registry_declares_historical_and_listing_contracts(self):
        validate_registry()
        definitions = {source.source_id: source for source in SOURCE_REGISTRY}
        law = definitions["maharashtra_law_judiciary"]
        assert law.listing_strategy == "PUBLIC_S3WAAS_SEARCH_WITH_PAGINATED_HTML_INDEX"
        assert law.listing_max_pages == 150
        assert isinstance(
            MaharashtraS3WaaSDocumentSource(law, cache_dir=Path("unused-v6-test-cache")),
            MaharashtraS3WaaSDocumentSource,
        )
        india_code = definitions["india_code"]
        assert india_code.base_host == "indiacode.gov.in"
        assert IndiaCodeSource.SEARCH_ROOT.endswith("/server/api/discover/search/objects")

    def test_corrected_amendment_identity_is_not_collapsed_to_principal_act(self):
        identity = "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_SECOND_AMENDMENT_ACT_2022"
        reference = _reference(identity)
        assert expected_identifier(reference) == identity
        assert generate_queries(reference) == [identity.replace("_", " ")]

    def test_s3waas_title_filter_preserves_version_and_year(self):
        expected = "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_SECOND_AMENDMENT_ACT_2022"
        score = MaharashtraS3WaaSDocumentSource._legal_title_score(
            expected,
            "Zilla Parishads and Panchayat Samitis (Second Amendment) Act, 2022. Mah. Act No. XLVII of 2022",
            "ACT",
        )
        wrong_version = MaharashtraS3WaaSDocumentSource._legal_title_score(
            expected,
            "Zilla Parishads and Panchayat Samitis (Amendment) Act, 2022",
            "ACT",
        )
        wrong_year = MaharashtraS3WaaSDocumentSource._legal_title_score(
            expected,
            "Zilla Parishads and Panchayat Samitis (Second Amendment) Act, 2016",
            "ACT",
        )
        wrong_kind = MaharashtraS3WaaSDocumentSource._legal_title_score(
            expected,
            "Zilla Parishads and Panchayat Samitis (Second Amendment) Ordinance, 2022",
            "ACT",
        )
        assert score >= 0.75
        assert wrong_version == 0.0
        assert wrong_year == 0.0
        assert wrong_kind == 0.0

    def test_s3waas_queries_include_observed_official_index_spelling(self):
        definition = next(source for source in SOURCE_REGISTRY if source.source_id == "maharashtra_law_judiciary")
        adapter = MaharashtraS3WaaSDocumentSource(definition, cache_dir=Path("unused-v6-test-cache"))
        reference = _reference(
            "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_SECOND_AMENDMENT_ACT_2016"
        )
        queries = adapter.build_queries(reference)
        assert len(queries) <= 3
        assert "ZILLA PARISHANDS AND PANCHAYAT SAMITIS SECOND AMENDMENT ACT 2016" in queries
