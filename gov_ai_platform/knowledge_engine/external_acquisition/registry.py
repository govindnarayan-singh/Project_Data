from __future__ import annotations

from .models import AuthorityLevel, SourceDefinition


SOURCE_REGISTRY = (
    SourceDefinition(
        adapter="maharashtra_gr",
        name="Maharashtra Government Resolutions",
        base_url="https://gr.maharashtra.gov.in/1145/Government-Resolutions",
        domains=("gr.maharashtra.gov.in",),
        authority=AuthorityLevel.OFFICIAL_PRIMARY,
        supported_kinds=(
            "GOVERNMENT_RESOLUTION", "CIRCULAR", "GOVERNMENT_CIRCULAR", "NOTIFICATION",
            "GOVERNMENT_LETTER", "DEPARTMENT_LETTER", "GOVERNMENT_ORDER", "CORRIGENDUM", "SUPPLEMENT",
        ),
        automated_discovery=False,
        block_reason="The official filtered search requires a case-sensitive CAPTCHA; V1 does not bypass it.",
    ),
    SourceDefinition(
        adapter="india_code",
        name="India Code",
        base_url="https://www.indiacode.nic.in/",
        domains=("www.indiacode.nic.in", "indiacode.nic.in", "upload.indiacode.nic.in"),
        authority=AuthorityLevel.OFFICIAL_PRIMARY,
        supported_kinds=("ACT", "RULE"),
    ),
    SourceDefinition(
        adapter="maharashtra_gazette",
        name="Maharashtra e-Gazette",
        base_url="https://egazzete.mahaonline.gov.in/Forms/GazetteSearch.aspx",
        domains=("egazzete.mahaonline.gov.in", "dgps.maharashtra.gov.in"),
        authority=AuthorityLevel.OFFICIAL_PRIMARY,
        supported_kinds=("NOTIFICATION", "ACT", "RULE", "COURT_ORDER"),
        automated_discovery=False,
        block_reason="The official stateful search form requires date/division fields; no undocumented endpoint is used.",
    ),
)


def sources_for_kind(kind: str) -> tuple[SourceDefinition, ...]:
    kind = (kind or "UNKNOWN").upper()
    return tuple(source for source in SOURCE_REGISTRY if source.enabled and kind in source.supported_kinds)


def validate_registry() -> None:
    adapters = [source.adapter for source in SOURCE_REGISTRY]
    if len(adapters) != len(set(adapters)):
        raise ValueError("Source adapter names must be unique")
    for source in SOURCE_REGISTRY:
        if not source.base_url.startswith("https://"):
            raise ValueError(f"Official source must use HTTPS: {source.adapter}")
        if source.authority == AuthorityLevel.NON_AUTHORITATIVE and source.enabled:
            raise ValueError("NON_AUTHORITATIVE sources cannot be enabled by default")


__all__ = ["SOURCE_REGISTRY", "sources_for_kind", "validate_registry"]
