from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from knowledge_engine.settings import settings

from .identity import verify_identity
from .models import ReferenceIdentity, SourceCandidate, SourceDefinition
from .network import NetworkSafetyError, SafeHttpClient
from .queries import generate_queries


class ExternalDocumentSource(Protocol):
    definition: SourceDefinition

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]: ...


class BlockedOfficialSource:
    def __init__(self, definition: SourceDefinition):
        self.definition = definition

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        query = generate_queries(reference)[0] if generate_queries(reference) else reference.normalized_identifier
        return [
            SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.adapter,
                source_domain=self.definition.domains[0],
                source_authority=self.definition.authority.value,
                discovery_query=query,
                discovery_status="BLOCKED",
                acquisition_status="BLOCKED",
                verification_status="BLOCKED",
                raw_source_evidence={"reason": self.definition.block_reason},
            )
        ]


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = ""
            self._text = []


class IndiaCodeSource:
    def __init__(self, definition: SourceDefinition, *, cache_dir: Path):
        self.definition = definition
        self.client = SafeHttpClient(
            allowed_domains=definition.domains,
            cache_dir=cache_dir,
            timeout_seconds=settings.external_http_timeout_seconds,
            max_bytes=settings.external_http_max_bytes,
            max_redirects=settings.external_http_max_redirects,
            retry_count=settings.external_http_retry_count,
            rate_delay_seconds=settings.external_http_rate_delay_seconds,
            user_agent=settings.external_http_user_agent,
        )

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        query = generate_queries(reference)[0]
        url = (
            "https://www.indiacode.nic.in/handle/123456789/1362/simple-search?"
            + urllib.parse.urlencode({"query": query, "searchradio": "all"})
        )
        try:
            fetched = self.client.fetch(url)
        except NetworkSafetyError as error:
            return [
                SourceCandidate(
                    reference.target_key, self.definition.adapter, self.definition.domains[0],
                    self.definition.authority.value, query, "NETWORK_FAILED", source_url=url,
                    acquisition_status="BLOCKED", verification_status="BLOCKED",
                    raw_source_evidence={"error": str(error)},
                )
            ]
        parser = _LinkParser()
        parser.feed(fetched.body.decode("utf-8", errors="replace"))
        candidates: list[SourceCandidate] = []
        seen: set[str] = set()
        for href, title in parser.links:
            if not title or "/handle/" not in href or "simple-search" in href:
                continue
            source_url = urllib.parse.urljoin(fetched.final_url, html.unescape(href))
            if source_url in seen:
                continue
            seen.add(source_url)
            match = verify_identity(reference, discovered_title=title)
            if match.status == "MISMATCH":
                continue
            verified = match.status in {"MATCH_EXACT", "MATCH_STRONG"}
            candidates.append(
                SourceCandidate(
                    reference.target_key, self.definition.adapter, urllib.parse.urlparse(source_url).hostname or "",
                    self.definition.authority.value, query, "DISCOVERED", source_url=source_url,
                    final_url=source_url, discovered_title=title, discovered_identifier=match.discovered_identifier,
                    identity_match_status=match.status, identity_evidence=match.evidence,
                    acquisition_status="AUTO_VERIFIED" if verified else "AMBIGUOUS",
                    verification_status="AUTO_VERIFIED" if verified else "UNVERIFIED",
                    raw_source_evidence={"search_url": url},
                )
            )
            if len(candidates) >= 5:
                break
        if candidates:
            return candidates
        return [
            SourceCandidate(
                reference.target_key, self.definition.adapter, self.definition.domains[0],
                self.definition.authority.value, query, "NOT_FOUND", source_url=url,
                acquisition_status="NOT_FOUND", verification_status="UNVERIFIED",
                raw_source_evidence={"search_url": url, "candidate_links_examined": len(parser.links)},
            )
        ]


__all__ = ["BlockedOfficialSource", "ExternalDocumentSource", "IndiaCodeSource"]
