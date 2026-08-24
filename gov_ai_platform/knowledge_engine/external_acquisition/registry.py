from __future__ import annotations

import urllib.parse

from .models import AcquisitionStrategy, AuthorityLevel, RatePolicy, SourceDefinition


SOURCE_REGISTRY = (
    SourceDefinition(
        source_id="maharashtra_gr",
        source_name="Maharashtra Government Resolutions",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India",
        supported_document_kinds=(
            "GOVERNMENT_RESOLUTION", "CIRCULAR", "GOVERNMENT_CIRCULAR", "NOTIFICATION",
            "GOVERNMENT_LETTER", "DEPARTMENT_LETTER", "GOVERNMENT_ORDER", "CORRIGENDUM", "SUPPLEMENT",
        ),
        supported_strategies=(
            AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
            AcquisitionStrategy.MAHARASHTRA_CIRCULAR,
            AcquisitionStrategy.MAHARASHTRA_NOTIFICATION,
            AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER,
            AcquisitionStrategy.OTHER_OFFICIAL_ADMIN_DOCUMENT,
        ),
        base_url="https://gr.maharashtra.gov.in/1145/Government-Resolutions",
        base_host="gr.maharashtra.gov.in",
        domains=("gr.maharashtra.gov.in",),
        discovery_mechanism="Official filtered search form",
        metadata_mechanism="Official result/landing metadata",
        download_mechanism="Official document link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        automated_discovery=False,
        block_reason="Official filtered search requires a case-sensitive CAPTCHA; automation does not bypass it.",
        search_executable=False,
        coverage_confirmed=True,
        coverage_evidence_url="https://gr.maharashtra.gov.in/1145/Government-Resolutions",
    ),
    SourceDefinition(
        source_id="india_code",
        source_name="India Code",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="India (central and indexed state legislation)",
        supported_document_kinds=("ACT", "RULE"),
        supported_strategies=(AcquisitionStrategy.INDIA_ACT, AcquisitionStrategy.INDIA_RULE),
        base_url="https://www.indiacode.nic.in/",
        base_host="www.indiacode.nic.in",
        domains=("www.indiacode.nic.in", "indiacode.nic.in", "upload.indiacode.nic.in"),
        discovery_mechanism="Official deterministic simple-search endpoint",
        metadata_mechanism="Official Act landing page",
        download_mechanism="Official /bitstream/ PDF link",
        rate_policy=RatePolicy(request_delay_seconds=0.75),
        coverage_start_year=1834,
        coverage_confirmed=True,
        coverage_evidence_url="https://www.indiacode.nic.in/",
    ),
    SourceDefinition(
        source_id="divyang_sahayak",
        source_name="Maharashtra Divyang Sahayak Government Resolution Index",
        authority_class=AuthorityLevel.OFFICIAL_SECONDARY,
        jurisdiction="Maharashtra, India (disability administration)",
        supported_document_kinds=(
            "GOVERNMENT_RESOLUTION", "CIRCULAR", "GOVERNMENT_CIRCULAR", "GOVERNMENT_ORDER",
        ),
        supported_strategies=(
            AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
            AcquisitionStrategy.MAHARASHTRA_CIRCULAR,
            AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER,
        ),
        base_url="https://divyangsahayakportal.maharashtra.gov.in/site/government-resolutions",
        base_host="divyangsahayakportal.maharashtra.gov.in",
        domains=("divyangsahayakportal.maharashtra.gov.in",),
        discovery_mechanism="Public official department GR index, filtered by published date and PDF identity",
        metadata_mechanism="Official index title/date plus parsed first-page administrative identity",
        download_mechanism="Official public /uploads/GR/ PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("persons with disabilities welfare department",),
        coverage_start_year=2000,
        coverage_confirmed=True,
        coverage_evidence_url="https://divyangsahayakportal.maharashtra.gov.in/site/government-resolutions",
    ),
    SourceDefinition(
        source_id="maharashtra_finance",
        source_name="Maharashtra Finance Department Documents",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (Finance Department)",
        supported_document_kinds=("GOVERNMENT_RESOLUTION", "GOVERNMENT_ORDER", "CIRCULAR", "GOVERNMENT_CIRCULAR"),
        supported_strategies=(AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION, AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER, AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        base_url="https://finance.maharashtra.gov.in/en/documents/",
        base_host="finance.maharashtra.gov.in",
        domains=("finance.maharashtra.gov.in", "cdnbbsr.s3waas.gov.in", "s349d4b2faeb4b7b9e745775793141e2b2.s3waas.gov.in"),
        discovery_mechanism="Official department exact-query document search",
        metadata_mechanism="Official document landing page",
        download_mechanism="Official S3WaaS PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("finance department",),
        coverage_confirmed=True,
        coverage_evidence_url="https://finance.maharashtra.gov.in/en/documents/",
    ),
    SourceDefinition(
        source_id="maharashtra_parliamentary_affairs",
        source_name="Maharashtra Parliamentary Affairs Department Documents",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (Parliamentary Affairs Department)",
        supported_document_kinds=("GOVERNMENT_RESOLUTION", "GOVERNMENT_ORDER", "CIRCULAR", "GOVERNMENT_CIRCULAR"),
        supported_strategies=(AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION, AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER, AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        base_url="https://pa.maharashtra.gov.in/en/documents/",
        base_host="pa.maharashtra.gov.in",
        domains=("pa.maharashtra.gov.in", "cdnbbsr.s3waas.gov.in", "s3b2d9368c9246f08e3b536d74b8a2f2ce.s3waas.gov.in"),
        discovery_mechanism="Official department exact-query document search",
        metadata_mechanism="Official document landing page",
        download_mechanism="Official S3WaaS PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("parliamentary affairs department",),
        coverage_confirmed=True,
        coverage_evidence_url="https://pa.maharashtra.gov.in/en/documents/",
    ),
    SourceDefinition(
        source_id="maharashtra_environment",
        source_name="Maharashtra Environment Department Documents",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (Environment Department)",
        supported_document_kinds=("GOVERNMENT_RESOLUTION", "GOVERNMENT_ORDER", "CIRCULAR", "GOVERNMENT_CIRCULAR"),
        supported_strategies=(AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION, AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER, AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        base_url="https://envd.maharashtra.gov.in/",
        base_host="envd.maharashtra.gov.in",
        domains=("envd.maharashtra.gov.in",),
        discovery_mechanism="Official department exact-query public search",
        metadata_mechanism="Official department landing page",
        download_mechanism="Official department PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("environment department",),
        coverage_confirmed=True,
        coverage_evidence_url="https://envd.maharashtra.gov.in/documents/",
    ),
    SourceDefinition(
        source_id="maharashtra_marathi_language",
        source_name="Maharashtra Marathi Language Department Documents",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (Marathi Language Department)",
        supported_document_kinds=("GOVERNMENT_RESOLUTION", "GOVERNMENT_ORDER", "CIRCULAR", "GOVERNMENT_CIRCULAR"),
        supported_strategies=(AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION, AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER, AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        base_url="https://marathi.gov.in/show/gr/",
        base_host="marathi.gov.in",
        domains=("marathi.gov.in",),
        discovery_mechanism="Official department exact-query WordPress search",
        metadata_mechanism="Official department post metadata",
        download_mechanism="Official department wp-content PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("marathi language department",),
        coverage_confirmed=True,
        coverage_evidence_url="https://marathi.gov.in/show/gr/",
    ),
    SourceDefinition(
        source_id="maharashtra_it",
        source_name="Maharashtra Information Technology Department Documents",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (Information Technology Department)",
        supported_document_kinds=("GOVERNMENT_RESOLUTION", "GOVERNMENT_ORDER", "CIRCULAR", "GOVERNMENT_CIRCULAR"),
        supported_strategies=(AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION, AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER, AcquisitionStrategy.MAHARASHTRA_CIRCULAR),
        base_url="https://it.maharashtra.gov.in/",
        base_host="it.maharashtra.gov.in",
        domains=("it.maharashtra.gov.in",),
        discovery_mechanism="Official department exact-query Drupal search",
        metadata_mechanism="Official department result metadata",
        download_mechanism="Official department /sites/default/files PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("electronics information technology and artificial intelligence department",),
        coverage_confirmed=True,
        coverage_evidence_url="https://it.maharashtra.gov.in/",
    ),
    SourceDefinition(
        source_id="maharashtra_law_judiciary",
        source_name="Maharashtra Law and Judiciary Department Act List",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India (state legislation)",
        supported_document_kinds=("ACT", "RULE"),
        supported_strategies=(AcquisitionStrategy.INDIA_ACT, AcquisitionStrategy.INDIA_RULE),
        base_url="https://lj.maharashtra.gov.in/en/document-category/act-list/",
        base_host="lj.maharashtra.gov.in",
        domains=("lj.maharashtra.gov.in", "cdnbbsr.s3waas.gov.in"),
        discovery_mechanism="Official department exact-query public search",
        metadata_mechanism="Official Act List result metadata",
        download_mechanism="Official S3WaaS PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        supported_issuers=("law and judiciary department", "maharashtra legislature"),
        coverage_start_year=1860,
        coverage_confirmed=True,
        coverage_evidence_url="https://lj.maharashtra.gov.in/en/document-category/act-list/",
    ),
    SourceDefinition(
        source_id="maharashtra_gazette",
        source_name="Maharashtra e-Gazette",
        authority_class=AuthorityLevel.OFFICIAL_PRIMARY,
        jurisdiction="Maharashtra, India",
        supported_document_kinds=("NOTIFICATION", "ACT", "RULE", "COURT_ORDER"),
        supported_strategies=(
            AcquisitionStrategy.MAHARASHTRA_NOTIFICATION,
            AcquisitionStrategy.COURT_DOCUMENT,
        ),
        base_url="https://egazzete.mahaonline.gov.in/Forms/GazetteSearch.aspx",
        base_host="egazzete.mahaonline.gov.in",
        domains=("egazzete.mahaonline.gov.in", "dgps.maharashtra.gov.in"),
        discovery_mechanism="Official stateful search form",
        metadata_mechanism="Official gazette result metadata",
        download_mechanism="Official gazette PDF link",
        rate_policy=RatePolicy(request_delay_seconds=1.0),
        automated_discovery=False,
        block_reason="Official stateful form requires date/division fields; no undocumented endpoint is used.",
        search_executable=False,
        coverage_confirmed=True,
        coverage_evidence_url="https://egazzete.mahaonline.gov.in/Forms/GazetteSearch.aspx",
    ),
)


def classify_strategy(kind: str, identifier: str = "") -> AcquisitionStrategy:
    canonical = (kind or "UNKNOWN").strip().upper()
    return {
        "GOVERNMENT_RESOLUTION": AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
        "CIRCULAR": AcquisitionStrategy.MAHARASHTRA_CIRCULAR,
        "GOVERNMENT_CIRCULAR": AcquisitionStrategy.MAHARASHTRA_CIRCULAR,
        "NOTIFICATION": AcquisitionStrategy.MAHARASHTRA_NOTIFICATION,
        "GOVERNMENT_ORDER": AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_ORDER,
        "ACT": AcquisitionStrategy.INDIA_ACT,
        "RULE": AcquisitionStrategy.INDIA_RULE,
        "COURT_ORDER": AcquisitionStrategy.COURT_DOCUMENT,
        "GOVERNMENT_LETTER": AcquisitionStrategy.OTHER_OFFICIAL_ADMIN_DOCUMENT,
        "DEPARTMENT_LETTER": AcquisitionStrategy.OTHER_OFFICIAL_ADMIN_DOCUMENT,
        "CORRIGENDUM": AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
        "SUPPLEMENT": AcquisitionStrategy.MAHARASHTRA_GOVERNMENT_RESOLUTION,
    }.get(canonical, AcquisitionStrategy.UNKNOWN)


def sources_for_kind(kind: str) -> tuple[SourceDefinition, ...]:
    canonical = (kind or "UNKNOWN").strip().upper()
    return tuple(
        source for source in SOURCE_REGISTRY
        if source.enabled and canonical in source.supported_document_kinds
    )


def sources_for_strategy(strategy: AcquisitionStrategy) -> tuple[SourceDefinition, ...]:
    return tuple(
        source for source in SOURCE_REGISTRY
        if source.enabled and strategy in source.supported_strategies
    )


def validate_registry() -> None:
    source_ids = [source.source_id for source in SOURCE_REGISTRY]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Source IDs must be unique")
    for source in SOURCE_REGISTRY:
        parsed = urllib.parse.urlparse(source.base_url)
        if parsed.scheme not in source.allowed_url_schemes or parsed.scheme != "https":
            raise ValueError(f"Official source must use configured HTTPS: {source.source_id}")
        if (parsed.hostname or "").casefold() != source.base_host.casefold():
            raise ValueError(f"Source base host mismatch: {source.source_id}")
        if source.base_host.casefold() not in {item.casefold() for item in source.domains}:
            raise ValueError(f"Source base host is not allowlisted: {source.source_id}")
        if source.authority_class == AuthorityLevel.NON_AUTHORITATIVE and source.enabled:
            raise ValueError("NON_AUTHORITATIVE sources cannot be enabled")
        if source.rate_policy.retry_count < 0 or source.rate_policy.request_delay_seconds < 0:
            raise ValueError(f"Invalid rate policy: {source.source_id}")


__all__ = [
    "SOURCE_REGISTRY",
    "classify_strategy",
    "sources_for_kind",
    "sources_for_strategy",
    "validate_registry",
]
