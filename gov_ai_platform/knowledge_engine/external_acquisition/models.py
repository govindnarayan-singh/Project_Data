from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class AuthorityLevel(StrEnum):
    OFFICIAL_PRIMARY = "OFFICIAL_PRIMARY"
    OFFICIAL_SECONDARY = "OFFICIAL_SECONDARY"
    TRUSTED_GOVERNMENT_INDEX = "TRUSTED_GOVERNMENT_INDEX"
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"


class AcquisitionStrategy(StrEnum):
    MAHARASHTRA_GOVERNMENT_RESOLUTION = "MAHARASHTRA_GOVERNMENT_RESOLUTION"
    MAHARASHTRA_CIRCULAR = "MAHARASHTRA_CIRCULAR"
    MAHARASHTRA_NOTIFICATION = "MAHARASHTRA_NOTIFICATION"
    MAHARASHTRA_GOVERNMENT_ORDER = "MAHARASHTRA_GOVERNMENT_ORDER"
    INDIA_ACT = "INDIA_ACT"
    INDIA_RULE = "INDIA_RULE"
    CENTRAL_GOVERNMENT_DOCUMENT = "CENTRAL_GOVERNMENT_DOCUMENT"
    COURT_DOCUMENT = "COURT_DOCUMENT"
    OTHER_OFFICIAL_ADMIN_DOCUMENT = "OTHER_OFFICIAL_ADMIN_DOCUMENT"
    UNKNOWN = "UNKNOWN"


class TargetState(StrEnum):
    AUTO_VERIFIED_READY = "AUTO_VERIFIED_READY"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND_AUTHORITATIVE = "NOT_FOUND_AUTHORITATIVE"
    BLOCKED_BY_SOURCE = "BLOCKED_BY_SOURCE"
    BLOCKED_BY_ENVIRONMENT = "BLOCKED_BY_ENVIRONMENT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    UNPARSEABLE_REFERENCE = "UNPARSEABLE_REFERENCE"
    NETWORK_FAILED = "NETWORK_FAILED"
    UNPARSEABLE_DOWNLOAD = "UNPARSEABLE_DOWNLOAD"
    ARCHIVE_FAILED = "ARCHIVE_FAILED"


class HistoricalCoverage(StrEnum):
    CONFIRMED = "HISTORICAL_COVERAGE_CONFIRMED"
    PARTIAL = "HISTORICAL_COVERAGE_PARTIAL"
    CURRENT_ONLY = "CURRENT_ONLY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RatePolicy:
    request_delay_seconds: float = 0.75
    timeout_seconds: float = 15.0
    retry_count: int = 1
    backoff_seconds: float = 0.75


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    source_name: str
    authority_class: AuthorityLevel
    jurisdiction: str
    supported_document_kinds: tuple[str, ...]
    supported_strategies: tuple[AcquisitionStrategy, ...]
    base_url: str
    base_host: str
    domains: tuple[str, ...]
    discovery_mechanism: str
    metadata_mechanism: str
    download_mechanism: str
    allowed_url_schemes: tuple[str, ...] = ("https",)
    rate_policy: RatePolicy = field(default_factory=RatePolicy)
    enabled: bool = True
    automated_discovery: bool = True
    block_reason: str = ""
    supported_issuers: tuple[str, ...] = ()
    coverage_start_year: int | None = None
    coverage_end_year: int | None = None
    search_executable: bool = True
    coverage_confirmed: bool = False
    coverage_evidence_url: str = ""
    historical_coverage: HistoricalCoverage = HistoricalCoverage.UNKNOWN
    listing_strategy: str = ""
    listing_url: str = ""
    listing_max_pages: int = 0

    # Compatibility names used by the V1 repository and reports.
    @property
    def adapter(self) -> str:
        return self.source_id

    @property
    def name(self) -> str:
        return self.source_name

    @property
    def authority(self) -> AuthorityLevel:
        return self.authority_class

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return self.supported_document_kinds


@dataclass(frozen=True, slots=True)
class ReferenceIdentity:
    target_key: str
    reference_kind: str
    raw_identifier: str
    normalized_identifier: str
    reference_date: date | None = None
    issuer: str = ""
    mentions: int = 0
    citing_documents: int = 0
    relation_signals: tuple[str, ...] = field(default_factory=tuple)
    priority_score: int = 0
    priority_tier: str = ""
    strategy: AcquisitionStrategy = AcquisitionStrategy.UNKNOWN
    source_examples: tuple[str, ...] = field(default_factory=tuple)
    source_page_quality: str = ""
    local_resolution_state: str = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    target_key: str
    source_adapter: str
    source_domain: str
    source_authority: str
    discovery_query: str
    discovery_status: str
    discovery_url: str = ""
    landing_url: str = ""
    document_url: str = ""
    redirect_chain: tuple[str, ...] = field(default_factory=tuple)
    discovered_title: str = ""
    discovered_identifier: str = ""
    discovered_date: date | None = None
    discovered_issuer: str = ""
    discovered_document_kind: str = ""
    identity_match_status: str = "NOT_EVALUATED"
    identity_evidence: dict[str, Any] = field(default_factory=dict)
    acquisition_status: str = "PENDING_DISCOVERY"
    verification_status: str = "UNVERIFIED"
    target_state: str = ""
    local_acquired_path: str = ""
    sha256: str = ""
    content_type: str = ""
    file_size: int | None = None
    page_count: int | None = None
    duplicate_status: str = ""
    archive_source_root: str = ""
    archive_relative_path: str = ""
    safe_local_filename: str = ""
    original_remote_filename: str = ""
    archive_status: str = "NOT_ARCHIVED"
    archived_at: str = ""
    raw_source_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def source_url(self) -> str:
        return self.discovery_url

    @property
    def final_url(self) -> str:
        return self.landing_url

    @property
    def download_url(self) -> str:
        return self.document_url


__all__ = [
    "AcquisitionStrategy",
    "AuthorityLevel",
    "HistoricalCoverage",
    "RatePolicy",
    "ReferenceIdentity",
    "SourceCandidate",
    "SourceDefinition",
    "TargetState",
]
