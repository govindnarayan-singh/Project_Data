import hashlib
import io
import json
import socket
import urllib.error
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfWriter

from knowledge_engine.external_acquisition.adapters import (
    BlockedOfficialSource,
    IndiaCodeSource,
    _LinkParser,
    _official_urljoin,
)
from knowledge_engine.external_acquisition.identity import expected_identifier, legal_pdf_corroborates, verify_identity
from knowledge_engine.external_acquisition.models import (
    AcquisitionStrategy,
    ReferenceIdentity,
    SourceCandidate,
    TargetState,
)
from knowledge_engine.external_acquisition.network import (
    FetchResult,
    NetworkSafetyError,
    SafeHttpClient,
    _BoundedRedirectHandler,
)
from knowledge_engine.external_acquisition.queries import assert_public_query, generate_queries
from knowledge_engine.external_acquisition.registry import (
    SOURCE_REGISTRY,
    classify_strategy,
    sources_for_kind,
    sources_for_strategy,
    validate_registry,
)
from knowledge_engine.external_acquisition.service import ExternalAcquisitionService
from knowledge_engine.external_acquisition.storage import (
    safe_filename,
    stage_incoming_file,
    store_verified_file,
    validate_pdf,
)


FIXTURES = Path(__file__).parent / "fixtures" / "external"


def reference(kind="GOVERNMENT_RESOLUTION", identifier="abc-2020/1", **values):
    return ReferenceIdentity(
        target_key=f"{kind}|{identifier}",
        reference_kind=kind,
        raw_identifier=values.pop("raw_identifier", identifier),
        normalized_identifier=identifier,
        strategy=values.pop("strategy", classify_strategy(kind)),
        **values,
    )


def minimal_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_source_registry_has_full_contract_and_only_authoritative_https_sources():
    validate_registry()
    assert all(item.base_url.startswith("https://") for item in SOURCE_REGISTRY)
    assert all(item.discovery_mechanism and item.metadata_mechanism and item.download_mechanism for item in SOURCE_REGISTRY)
    assert all(item.rate_policy.retry_count >= 0 for item in SOURCE_REGISTRY)
    assert {item.adapter for item in sources_for_kind("ACT")} == {
        "india_code", "maharashtra_gazette", "maharashtra_law_judiciary"
    }
    assert {item.adapter for item in sources_for_strategy(AcquisitionStrategy.INDIA_ACT)} == {
        "india_code", "maharashtra_law_judiciary"
    }
    assert not any(item.authority.value == "NON_AUTHORITATIVE" and item.enabled for item in SOURCE_REGISTRY)


@pytest.mark.parametrize(
    ("kind", "strategy"),
    [
        ("GOVERNMENT_RESOLUTION", AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION),
        ("GOVERNMENT_CIRCULAR", AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        ("NOTIFICATION", AcquisitionStrategy.MAHARASHTRA_NOTIFICATION),
        ("GOVERNMENT_ORDER", AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER),
        ("ACT", AcquisitionStrategy.INDIA_ACT),
        ("RULE", AcquisitionStrategy.INDIA_RULE),
        ("COURT_ORDER", AcquisitionStrategy.COURT_DOCUMENT),
        ("UNKNOWN", AcquisitionStrategy.UNKNOWN),
    ],
)
def test_document_kind_routes_to_explicit_strategy(kind, strategy):
    assert classify_strategy(kind) == strategy


def test_query_generation_is_identifier_first_deterministic_and_minimal():
    item = reference(reference_date=date(2020, 2, 20), issuer="Finance Department")
    assert generate_queries(item) == [
        "abc-2020/1",
        "abc-2020/1 2020-02-20",
        "abc-2020/1 Finance Department",
    ]
    for query in generate_queries(item):
        assert_public_query(query)
    with pytest.raises(ValueError):
        assert_public_query(r"D:\private\file.pdf")
    with pytest.raises(ValueError):
        assert_public_query("x" * 241)


def test_act_query_uses_canonical_public_title_and_year():
    item = reference("ACT", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016")
    assert generate_queries(item) == ["Rights of Persons with Disabilities Act 2016"]


@pytest.mark.parametrize(
    ("identifier", "raw", "expected"),
    [
        ("1_पर्यावरण_संरक्षण_ACT_1986", "पर्यावरण (संरक्षण) अधिनियम, 1986", "ENVIRONMENT_PROTECTION_ACT_1986"),
        ("2_हवा_प्रदूषण_प्रतिबंध_व_नियंत्रण_ACT_1981", "हवा (प्रदूषण, प्रतिबंध व नियंत्रण) अधिनियम, 1981", "AIR_PREVENTION_AND_CONTROL_OF_POLLUTION_ACT_1981"),
        ("ACCORDING_TO_MAHARASHTRA_URBAN_AREAS_PROTECTION_PRESERVATION_OF_TREES_ACT_1975", "Maharashtra Urban Areas Protection and Preservation of Trees Act 1975", "MAHARASHTRA_URBAN_AREAS_PROTECTION_AND_PRESERVATION_OF_TREES_ACT_1975"),
    ],
)
def test_known_legal_targets_get_deterministic_canonical_identity(identifier, raw, expected):
    assert expected_identifier(reference("ACT", identifier, raw_identifier=raw)) == expected


def test_exact_strong_conflict_and_mismatch_gr_identity_rules():
    item = reference(reference_date=date(2020, 2, 20), issuer="Finance")
    exact = verify_identity(item, discovered_title="Official", discovered_identifier="abc-2020/1", discovered_date=date(2020, 2, 20), discovered_issuer="Finance")
    strong = verify_identity(item, discovered_title="Official", discovered_identifier="abc-2020/1")
    conflict = verify_identity(item, discovered_title="Official", discovered_identifier="abc-2020/1", discovered_date=date(2020, 2, 21))
    mismatch = verify_identity(item, discovered_title="Official", discovered_identifier="xyz-2020/1")
    assert (exact.status, strong.status, conflict.status, mismatch.status) == (
        "MATCH_EXACT", "MATCH_STRONG", "AMBIGUOUS", "MISMATCH"
    )


def test_act_and_rule_identity_are_title_year_not_vector_similarity():
    act = reference("ACT", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016")
    assert verify_identity(act, discovered_title="The Rights of Persons with Disabilities Act, 2016").status == "MATCH_EXACT"
    rule = reference("RULE", "CONSTRUCTION_AND_DEMOLITION_WASTE_MANAGEMENT_RULES_2016")
    assert verify_identity(rule, discovered_title="Construction and Demolition Waste Management Rules, 2016").status == "MATCH_EXACT"


def test_downloaded_legal_pdf_requires_title_tokens_and_year_corroboration():
    item = reference("ACT", "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_ACT_1961")
    passed, evidence = legal_pdf_corroborates(
        item,
        "THE MAHARASHTRA ZILLA PARISHADS AND PANCHAYAT SAMITIS ACT, 1961 (Maharashtra Act V of 1962)",
    )
    failed, _ = legal_pdf_corroborates(item, "Unrelated Maharashtra Act, 1961")
    assert passed and evidence["year_present_in_pdf_probe"]
    assert not failed


def test_blocked_adapter_preserves_captcha_as_source_not_environment_block():
    definition = next(item for item in SOURCE_REGISTRY if item.adapter == "maharashtra_gr")
    result = BlockedOfficialSource(definition).search(reference())[0]
    assert result.target_state == TargetState.BLOCKED_BY_SOURCE
    assert result.verification_status == "BLOCKED"
    assert "CAPTCHA" in result.raw_source_evidence["detail"]


def test_network_disabled_state_is_separate_from_not_found():
    definition = next(item for item in SOURCE_REGISTRY if item.adapter == "maharashtra_gr")
    result = ExternalAcquisitionService._environment_block(reference(), definition)
    assert result.target_state == TargetState.BLOCKED_BY_ENVIRONMENT
    assert result.discovery_status == "BLOCKED"
    assert "not available" in result.raw_source_evidence["detail"]


def test_network_client_rejects_scheme_credentials_unlisted_host_and_private_ip(tmp_path):
    client = SafeHttpClient(allowed_domains=("official.example", "127.0.0.1"), cache_dir=tmp_path)
    for url in (
        "http://official.example/file.pdf",
        "https://user:pass@official.example/file.pdf",
        "https://evil.example/file.pdf",
        "https://127.0.0.1/file.pdf",
    ):
        with pytest.raises(NetworkSafetyError):
            client._validate_url(url)


def test_dns_resolution_rejects_private_or_link_local_addresses(monkeypatch, tmp_path):
    client = SafeHttpClient(allowed_domains=("official.example",), cache_dir=tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443))])
    with pytest.raises(NetworkSafetyError):
        client._validate_url("https://official.example/file", resolve_dns=True)


def test_redirect_handler_blocks_cross_domain_and_bounds_redirects():
    handler = _BoundedRedirectHandler({"official.example"}, 1)
    with pytest.raises(NetworkSafetyError):
        handler.redirect_request(None, None, 302, "", {}, "https://evil.example/file")
    handler.redirects.append("https://official.example/one")
    with pytest.raises(NetworkSafetyError):
        handler.redirect_request(None, None, 302, "", {}, "https://official.example/two")


def test_pdf_payload_validation_rejects_html_invalid_pdf_and_empty():
    with pytest.raises(NetworkSafetyError):
        SafeHttpClient._validate_payload(b"<html>error</html>", "text/html", True)
    with pytest.raises(NetworkSafetyError):
        SafeHttpClient._validate_payload(b"not-pdf", "application/pdf", True)
    with pytest.raises(NetworkSafetyError):
        SafeHttpClient._validate_payload(b"", "text/html", False)
    with pytest.raises(ValueError):
        validate_pdf(b"%PDF-invalid")


def test_network_retries_and_timeout_are_bounded(monkeypatch, tmp_path):
    class FailingOpener:
        def __init__(self): self.calls = 0
        def open(self, request, timeout):
            self.calls += 1
            assert timeout == 2
            raise urllib.error.URLError("timed out")

    opener = FailingOpener()
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: opener)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    client = SafeHttpClient(allowed_domains=("official.example",), cache_dir=tmp_path, timeout_seconds=2, retry_count=1, retry_backoff_seconds=0, rate_delay_seconds=0)
    with pytest.raises(NetworkSafetyError):
        client.fetch("https://official.example/document")
    assert opener.calls == 2


def test_response_size_is_bounded_before_body_read(monkeypatch, tmp_path):
    class Headers:
        def get_content_type(self): return "application/pdf"
        def get(self, key): return "999" if key == "Content-Length" else None
    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def geturl(self): return "https://official.example/file.pdf"
        def getcode(self): return 200
    class Opener:
        def open(self, request, timeout): return Response()
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    client = SafeHttpClient(allowed_domains=("official.example",), cache_dir=tmp_path, max_bytes=100, retry_count=0, rate_delay_seconds=0)
    with pytest.raises(NetworkSafetyError, match="maximum size"):
        client.fetch("https://official.example/file.pdf", expected_pdf=True)


def test_cache_uses_distinct_atomic_body_and_metadata_files(monkeypatch, tmp_path):
    body = b"official html"
    class Headers:
        def get_content_type(self): return "text/html"
        def get(self, key): return str(len(body)) if key == "Content-Length" else None
    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def geturl(self): return "https://official.example/page"
        def getcode(self): return 200
        def read(self, maximum): return body
    class Opener:
        calls = 0
        def open(self, request, timeout): self.calls += 1; return Response()
    opener = Opener()
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: opener)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    client = SafeHttpClient(allowed_domains=("official.example",), cache_dir=tmp_path, retry_count=0, rate_delay_seconds=0)
    first = client.fetch("https://official.example/page")
    second = client.fetch("https://official.example/page")
    assert first.body == second.body == body
    assert not first.from_cache and second.from_cache
    assert opener.calls == 1
    assert len(list(tmp_path.glob("*.bin"))) == len(list(tmp_path.glob("*.json"))) == 1


def test_safe_quarantine_storage_parses_pdf_is_immutable_and_deduplicated(tmp_path):
    body = minimal_pdf()
    probe = validate_pdf(body)
    assert probe.page_count == 1
    first, digest, created = store_verified_file(tmp_path, identifier="../../unsafe name", body=body)
    second, second_digest, second_created = store_verified_file(tmp_path, identifier="../../unsafe name", body=body)
    assert first == second and digest == second_digest
    assert created and not second_created
    assert first.parent == tmp_path.resolve()
    assert ".." not in first.name


def test_storage_rejects_non_pdf_and_server_filename_is_never_used(tmp_path):
    assert safe_filename("a/b\\c") == "a_b_c.pdf"
    with pytest.raises(ValueError):
        store_verified_file(tmp_path, identifier="bad", body=b"MZ executable")


def test_incoming_stages_bytes_before_local_pdf_verification(tmp_path):
    incoming, digest, created = stage_incoming_file(
        tmp_path, identifier="../../unsafe name", body=b"MZ executable"
    )
    assert created is True
    assert incoming.parent == tmp_path.resolve()
    assert hashlib.sha256(incoming.read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="PDF signature"):
        validate_pdf(incoming.read_bytes())


def test_mock_search_and_landing_fixtures_expose_supported_links():
    search_parser = _LinkParser()
    search_parser.feed((FIXTURES / "india_code_search.html").read_text(encoding="utf-8"))
    assert any("ViewSelectedActDetailsServlet" in href for href, _, _ in search_parser.links)
    landing_parser = _LinkParser()
    landing_parser.feed((FIXTURES / "india_code_landing.html").read_text(encoding="utf-8"))
    assert any("/bitstream/" in href for href, _, _ in landing_parser.links)


def test_official_html_url_join_encodes_spaces_and_preserves_query_values():
    url = _official_urljoin(
        "https://www.indiacode.nic.in/search",
        "/ViewSelectedActDetailsServlet?act_name=The Rights Act, 2016.&handleid=123/4",
    )
    assert " " not in url
    assert "act_name=The+Rights+Act%2C+2016." in url
    assert "handleid=123%2F4" in url


def test_item_metadata_selects_primary_official_original_bundle_pdf(tmp_path):
    definition = next(item for item in SOURCE_REGISTRY if item.adapter == "india_code")
    adapter = IndiaCodeSource(definition, cache_dir=tmp_path)
    def fetch(url, expected_pdf=False):
        if "/bundles?" in url:
            payload = {"_embedded": {"bundles": [{
                "name": "ORIGINAL",
                "_links": {"bitstreams": {"href": "https://indiacode.gov.in/server/api/core/bundles/b/bitstreams"}},
            }]}}
        elif "/items/" in url:
            payload = {"_links": {"bundles": {"href": "https://indiacode.gov.in/server/api/core/items/x/bundles"}}}
        else:
            payload = {"_embedded": {"bitstreams": [{
                "name": "a2016-49.pdf", "sizeBytes": 100,
                "_links": {"content": {"href": "https://indiacode.gov.in/server/api/core/bitstreams/p/content"}},
            }]}}
        return FetchResult(url, url, "application/json", json.dumps(payload).encode())
    adapter.client.fetch = fetch
    candidate = SourceCandidate(
        target_key="ACT|x", source_adapter="india_code", source_domain="indiacode.gov.in",
        source_authority="OFFICIAL_PRIMARY", discovery_query="x", discovery_status="DISCOVERED",
        landing_url="https://indiacode.gov.in/server/api/core/items/x",
        discovered_title="The Rights of Persons with Disabilities Act, 2016.",
    )
    result = adapter.fetch_candidate_metadata(candidate)
    assert result.document_url.endswith("/server/api/core/bitstreams/p/content")
    assert result.raw_source_evidence["official_bundle"] == "ORIGINAL"
    assert adapter.verify_authority(result)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([TargetState.AUTO_VERIFIED_READY], TargetState.AUTO_VERIFIED_READY),
        ([TargetState.IDENTITY_MISMATCH, TargetState.AMBIGUOUS], TargetState.AMBIGUOUS),
        ([TargetState.BLOCKED_BY_SOURCE], TargetState.BLOCKED_BY_SOURCE),
        ([TargetState.NOT_FOUND_AUTHORITATIVE], TargetState.NOT_FOUND_AUTHORITATIVE),
    ],
)
def test_every_target_collapses_to_one_defensible_high_level_state(states, expected):
    rows = [SourceCandidate("k", "a", "h", "OFFICIAL_PRIMARY", "q", "DISCOVERED", target_state=state) for state in states]
    assert ExternalAcquisitionService._target_state(rows) == expected


def test_source_disagreement_is_deterministic():
    def verified(adapter, digest, identifier="ACT_2020"):
        return SourceCandidate("k", adapter, "h", "OFFICIAL_PRIMARY", "q", "DISCOVERED", discovered_identifier=identifier, verification_status="AUTO_VERIFIED", sha256=digest)
    assert ExternalAcquisitionService._source_disagreement([verified("a", "1"), verified("b", "1")]) == "SAME_DOCUMENT_DUPLICATE_SOURCE"
    assert ExternalAcquisitionService._source_disagreement([verified("a", "1"), verified("b", "2")]) == "VERSION_VARIANT"
    assert ExternalAcquisitionService._source_disagreement([verified("a", "1"), verified("b", "2", "OTHER")]) == "CONFLICTING_CANDIDATES"


def test_additive_external_migration_never_inserts_search_documents():
    sql = (Path(__file__).parents[1] / "deployment" / "postgres" / "migrations" / "004_external_acquisition_v1.sql").read_text(encoding="utf-8").casefold()
    assert "external_acquisition_candidates" in sql
    assert "insert into documents" not in sql
    assert "human_confirmed" not in sql
