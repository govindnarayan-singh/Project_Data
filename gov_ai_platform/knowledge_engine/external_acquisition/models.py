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


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    adapter: str
    name: str
    base_url: str
    domains: tuple[str, ...]
    authority: AuthorityLevel
    supported_kinds: tuple[str, ...]
    enabled: bool = True
    automated_discovery: bool = True
    block_reason: str = ""


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


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    target_key: str
    source_adapter: str
    source_domain: str
    source_authority: str
    discovery_query: str
    discovery_status: str
    source_url: str = ""
    final_url: str = ""
    download_url: str = ""
    discovered_title: str = ""
    discovered_identifier: str = ""
    discovered_date: date | None = None
    identity_match_status: str = "NOT_EVALUATED"
    identity_evidence: dict[str, Any] = field(default_factory=dict)
    acquisition_status: str = "PENDING_DISCOVERY"
    verification_status: str = "UNVERIFIED"
    raw_source_evidence: dict[str, Any] = field(default_factory=dict)

