from datetime import date
from pathlib import Path
import urllib.error

import pytest

from knowledge_engine.external_acquisition.adapters import BlockedOfficialSource
from knowledge_engine.external_acquisition.identity import verify_identity
from knowledge_engine.external_acquisition.models import ReferenceIdentity
from knowledge_engine.external_acquisition.network import NetworkSafetyError, SafeHttpClient, _BoundedRedirectHandler
from knowledge_engine.external_acquisition.queries import generate_queries
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY, sources_for_kind, validate_registry
from knowledge_engine.external_acquisition.storage import safe_filename, store_verified_file


def reference(kind="GOVERNMENT_RESOLUTION", identifier="abc-2020/1", **values):
    return ReferenceIdentity(
        target_key=f"{kind}|{identifier}",
        reference_kind=kind,
        raw_identifier=values.pop("raw_identifier", identifier),
        normalized_identifier=identifier,
        **values,
    )


def test_source_registry_is_unique_https_official_and_routes_legal_kinds():
    validate_registry()
    assert all(item.base_url.startswith("https://") for item in SOURCE_REGISTRY)
    assert {item.adapter for item in sources_for_kind("ACT")} == {"india_code", "maharashtra_gazette"}
    assert not any(item.authority.value == "NON_AUTHORITATIVE" and item.enabled for item in SOURCE_REGISTRY)


def test_query_generation_is_deterministic_bounded_and_not_llm_generated():
    item = reference(reference_date=date(2020, 2, 20), issuer="Finance Department")
    assert generate_queries(item) == [
        "abc-2020/1",
        "abc-2020/1 Finance Department",
        "abc-2020/1 2020-02-20",
    ]


def test_act_query_uses_canonical_title_and_year():
    item = reference("ACT", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016")
    assert generate_queries(item) == ["RIGHTS OF PERSONS WITH DISABILITIES ACT 2016"]


def test_exact_and_strong_gr_identity_matching():
    item = reference(reference_date=date(2020, 2, 20), issuer="Finance")
    exact = verify_identity(
        item,
        discovered_title="Official record",
        discovered_identifier="abc-2020/1",
        discovered_date=date(2020, 2, 20),
        discovered_issuer="Finance",
    )
    strong = verify_identity(item, discovered_title="Official record", discovered_identifier="abc-2020/1")
    assert exact.status == "MATCH_EXACT"
    assert strong.status == "MATCH_STRONG"


def test_identity_conflict_stays_ambiguous_and_mismatch_is_rejected():
    item = reference(reference_date=date(2020, 2, 20))
    assert verify_identity(
        item,
        discovered_title="Official record",
        discovered_identifier="abc-2020/1",
        discovered_date=date(2020, 2, 21),
    ).status == "AMBIGUOUS"
    assert verify_identity(item, discovered_title="Official record", discovered_identifier="xyz-2020/1").status == "MISMATCH"


def test_act_canonicalization_is_first_class_identity_proof():
    item = reference("ACT", "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016")
    match = verify_identity(item, discovered_title="The Rights of Persons with Disabilities Act, 2016")
    assert match.status == "MATCH_EXACT"


def test_blocked_official_adapter_preserves_captcha_boundary():
    definition = next(item for item in SOURCE_REGISTRY if item.adapter == "maharashtra_gr")
    result = BlockedOfficialSource(definition).search(reference())[0]
    assert result.discovery_status == "BLOCKED"
    assert result.verification_status == "BLOCKED"
    assert "CAPTCHA" in result.raw_source_evidence["reason"]


def test_network_client_rejects_scheme_credentials_and_unlisted_domain(tmp_path):
    client = SafeHttpClient(allowed_domains=("official.example",), cache_dir=tmp_path)
    for url in (
        "http://official.example/file.pdf",
        "https://user:pass@official.example/file.pdf",
        "https://evil.example/file.pdf",
    ):
        with pytest.raises(NetworkSafetyError):
            client._validate_url(url)


def test_redirect_handler_blocks_cross_domain_and_bounds_redirects():
    handler = _BoundedRedirectHandler({"official.example"}, 1)
    with pytest.raises(NetworkSafetyError):
        handler.redirect_request(None, None, 302, "", {}, "https://evil.example/file")
    handler.redirects = 1
    with pytest.raises(NetworkSafetyError):
        handler.redirect_request(None, None, 302, "", {}, "https://official.example/file")


def test_pdf_validation_rejects_html_and_error_responses():
    with pytest.raises(NetworkSafetyError):
        SafeHttpClient._validate_payload(b"<html>error</html>", "text/html", True)
    with pytest.raises(NetworkSafetyError):
        SafeHttpClient._validate_payload(b"not-pdf", "application/pdf", True)


def test_network_retries_are_bounded(monkeypatch, tmp_path):
    class FailingOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.URLError("offline")

    opener = FailingOpener()
    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: opener)
    client = SafeHttpClient(
        allowed_domains=("official.example",), cache_dir=tmp_path,
        retry_count=1, rate_delay_seconds=0,
    )
    with pytest.raises(NetworkSafetyError):
        client.fetch("https://official.example/document")
    assert opener.calls == 2


def test_safe_quarantine_storage_is_immutable_and_deduplicated(tmp_path):
    body = b"%PDF-1.4\nverified"
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
