from __future__ import annotations

import csv
import hashlib
import io
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter

from knowledge_engine.external_acquisition.kg_guided_v1 import (
    EXPECTED_TARGET_SHA256,
    TARGET_SOURCE,
    KgGuidedAcquisitionV1Service,
    graph_tier,
    potential_impact,
)
from knowledge_engine.external_acquisition.models import AcquisitionStrategy, ReferenceIdentity, SourceCandidate
from knowledge_engine.external_acquisition.network import FetchResult


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_frozen_queue_is_exactly_50_unique_ranked_targets_with_expected_sha():
    rows = list(csv.DictReader(TARGET_SOURCE.open(encoding="utf-8-sig")))
    assert len(rows) == 50
    assert sorted(int(row["rank"]) for row in rows) == list(range(1, 51))
    assert len({row["normalized_target_identity"] for row in rows}) == 50
    assert hashlib.sha256(TARGET_SOURCE.read_bytes()).hexdigest() == EXPECTED_TARGET_SHA256


def test_graph_tier_order_is_bridge_then_multi_then_relation_then_standard():
    assert graph_tier({"component_bridge_score": "1", "distinct_citing_documents": "9", "relation_signals": "AMENDS"})[0] == "TIER_A_GRAPH_BRIDGE"
    assert graph_tier({"component_bridge_score": "0", "distinct_citing_documents": "2", "relation_signals": "AMENDS"})[0] == "TIER_B_MULTI_CITED"
    assert graph_tier({"component_bridge_score": "0", "distinct_citing_documents": "1", "relation_signals": "CORRECTS"})[0] == "TIER_C_STRONG_RELATION"
    assert graph_tier({"component_bridge_score": "0", "distinct_citing_documents": "1", "relation_signals": ""})[0] == "TIER_D_STANDARD_REFERENCE"


def test_potential_impact_is_explicitly_non_actual_and_component_formula_is_bounded():
    value = potential_impact([{
        "distinct citing documents": "2", "citing_document_ids": "4;9",
        "potential_component_merges": "1", "relation_signals": "AMENDS",
    }])
    assert value["potential_only"] is True
    assert value["potential_incoming_references"] == 2
    assert value["distinct_citing_documents_affected"] == 2
    assert value["potential_component_merges_upper_bound"] == 1


def test_rights_act_ambiguity_is_preserved_before_network():
    state, _ = KgGuidedAcquisitionV1Service._quality(
        "ACT", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016", {}, set(), set(), set()
    )
    assert state == "AMBIGUOUS_TARGET_IDENTITY"


def test_fragment_and_heading_legal_identities_are_quality_blocked():
    fragment, _ = KgGuidedAcquisitionV1Service._quality("ACT", "POLLUTION_ACT_1974", {}, set(), set(), set())
    heading, _ = KgGuidedAcquisitionV1Service._quality("ACT", "AMENDMENTS_TO_THE_BIOLOGICAL_DIVERSITY_ACT_2002", {}, set(), set(), set())
    assert fragment == "INSUFFICIENT_IDENTITY"
    assert heading == "DAMAGED_IDENTIFIER"


def test_exact_database_and_catalogue_identities_skip_network():
    key = ("ACT", "EXAMPLE_DOCUMENT_ACT_2020")
    assert KgGuidedAcquisitionV1Service._quality(*key, {}, {key}, set(), set())[0] == "ALREADY_INGESTED"
    assert KgGuidedAcquisitionV1Service._quality(*key, {}, set(), {key}, set())[0] == "ALREADY_RESOLVED"
    assert KgGuidedAcquisitionV1Service._quality(*key, {}, set(), set(), {key})[0] == "ALREADY_EXTERNAL_VERIFIED"


def test_local_first_download_stages_before_pdf_identity_processing(tmp_path):
    body = _pdf()
    reference = ReferenceIdentity(
        target_key="ACT|EXAMPLE_DOCUMENT_ACT_2020", reference_kind="ACT",
        raw_identifier="Example Document Act, 2020", normalized_identifier="EXAMPLE_DOCUMENT_ACT_2020",
        strategy=AcquisitionStrategy.INDIA_ACT,
    )
    candidate = SourceCandidate(
        target_key=reference.target_key, source_adapter="official_test", source_domain="example.gov.in",
        source_authority="OFFICIAL_PRIMARY", discovery_query="EXAMPLE DOCUMENT ACT 2020",
        discovery_status="DISCOVERED", document_url="https://example.gov.in/example.pdf",
    )

    class Client:
        def fetch(self, url, *, expected_pdf=False):
            assert expected_pdf is False
            return FetchResult(url, url, "application/pdf", body)

    class Adapter:
        client = Client()

        @staticmethod
        def fetch_candidate_metadata(value):
            return value

        @staticmethod
        def verify_authority(value):
            return True

    fake_settings = SimpleNamespace(external_incoming_root=tmp_path)
    with (
        patch("knowledge_engine.external_acquisition.kg_guided_v1.settings", fake_settings),
        patch("knowledge_engine.external_acquisition.kg_guided_v1._tesseract_available", return_value=False),
    ):
        result = KgGuidedAcquisitionV1Service._download_local_first(reference, Adapter(), candidate, set(), set(), set())
    assert (tmp_path / result.local_acquired_path).read_bytes() == body
    assert result.identity_evidence["identity_source"] == "LOCAL_INCOMING_PDF"
    assert result.raw_source_evidence["remote_stream_identity_processing"] is False
    assert result.identity_match_status == "MISMATCH"


def test_runner_source_contains_no_backup_or_podman_execution_path():
    source = inspect.getsource(KgGuidedAcquisitionV1Service)
    assert "pg_dump" not in source
    assert "podman " not in source.casefold()
    assert "apply_migration" not in source


def test_manifest_validator_policy_names_only_permanent_archive_fields():
    source = inspect.getsource(KgGuidedAcquisitionV1Service.validate_manifest)
    assert "ARCHIVE_ROOT_IDENTITY" in source
    assert "incoming_paths" in source
    assert "remote_source_paths" in source
