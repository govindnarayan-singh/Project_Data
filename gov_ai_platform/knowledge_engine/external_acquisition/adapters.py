from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from knowledge_engine.settings import settings
from knowledge_engine.graph.normalization import normalize_document_number

from .identity import legal_identity_quality, verify_identity
from .models import ReferenceIdentity, SourceCandidate, SourceDefinition, TargetState
from .network import FetchResult, NetworkSafetyError, SafeHttpClient
from .queries import assert_public_query, generate_queries
from .storage import PdfProbe, validate_pdf


class ExternalDocumentSource(Protocol):
    definition: SourceDefinition

    def supports(self, reference: ReferenceIdentity) -> bool: ...

    def build_queries(self, reference: ReferenceIdentity) -> list[str]: ...

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]: ...

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate: ...

    def download(self, candidate: SourceCandidate) -> tuple[FetchResult, PdfProbe]: ...

    def verify_authority(self, candidate: SourceCandidate) -> bool: ...


class BlockedOfficialSource:
    def __init__(self, definition: SourceDefinition):
        self.definition = definition

    def supports(self, reference: ReferenceIdentity) -> bool:
        return reference.strategy in self.definition.supported_strategies

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        return generate_queries(reference)

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        queries = self.build_queries(reference)
        query = queries[0] if queries else reference.normalized_identifier
        return [
            SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host,
                source_authority=self.definition.authority_class.value,
                discovery_query=query,
                discovery_status="BLOCKED",
                acquisition_status="BLOCKED",
                verification_status="BLOCKED",
                target_state=TargetState.BLOCKED_BY_SOURCE.value,
                raw_source_evidence={
                    "reason": "BLOCKED_BY_SOURCE",
                    "detail": self.definition.block_reason,
                },
            )
        ]

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate:
        return candidate

    def download(self, candidate: SourceCandidate) -> tuple[FetchResult, PdfProbe]:
        raise NetworkSafetyError(self.definition.block_reason)

    def verify_authority(self, candidate: SourceCandidate) -> bool:
        return candidate.source_authority != "NON_AUTHORITATIVE"


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str, dict[str, str]]] = []
        self._href = ""
        self._attrs: dict[str, str] = {}
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "a":
            self._attrs = {str(key): str(value or "") for key, value in attrs}
            self._href = self._attrs.get("href", "").strip()
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split()), self._attrs))
            self._href = ""
            self._attrs = {}
            self._text = []


def _official_urljoin(base_url: str, href: str) -> str:
    """Join an official HTML href and encode unsafe display characters."""
    joined = urllib.parse.urljoin(base_url, html.unescape(href).strip())
    parts = urllib.parse.urlsplit(joined)
    query = urllib.parse.urlencode(
        urllib.parse.parse_qsl(parts.query, keep_blank_values=True),
        doseq=True,
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%"), query, "")
    )

class IndiaCodeSource:
    SEARCH_ROOT = "https://www.indiacode.nic.in/handle/123456789/1362/simple-search"

    def __init__(self, definition: SourceDefinition, *, cache_dir: Path):
        self.definition = definition
        policy = definition.rate_policy
        self.client = SafeHttpClient(
            allowed_domains=definition.domains,
            allowed_schemes=definition.allowed_url_schemes,
            cache_dir=cache_dir,
            timeout_seconds=settings.external_http_timeout_seconds or policy.timeout_seconds,
            max_bytes=settings.external_http_max_bytes,
            max_redirects=settings.external_http_max_redirects,
            retry_count=settings.external_http_retry_count,
            retry_backoff_seconds=policy.backoff_seconds,
            rate_delay_seconds=max(settings.external_http_rate_delay_seconds, policy.request_delay_seconds),
            user_agent=settings.external_http_user_agent,
        )

    def supports(self, reference: ReferenceIdentity) -> bool:
        return reference.strategy in self.definition.supported_strategies

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        queries = generate_queries(reference)
        for query in queries:
            assert_public_query(query)
        return queries

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        valid, reason = legal_identity_quality(reference.normalized_identifier)
        if reference.reference_kind in {"ACT", "RULE"} and not valid:
            return [SourceCandidate(
                target_key=reference.target_key, source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host,
                source_authority=self.definition.authority_class.value,
                discovery_query=(self.build_queries(reference) or [reference.normalized_identifier])[0],
                discovery_status="NOT_FOUND", acquisition_status="NOT_FOUND",
                target_state=TargetState.UNPARSEABLE_REFERENCE.value,
                raw_source_evidence={"reason": "IDENTIFIER_EXTRACTION_FAILURE", "quality_gate": reason},
            )]
        candidates: list[SourceCandidate] = []
        seen: set[str] = set()
        network_errors: list[str] = []
        for query in self.build_queries(reference):
            url = self.SEARCH_ROOT + "?" + urllib.parse.urlencode({"query": query, "searchradio": "all"})
            try:
                fetched = self.client.fetch(url)
            except NetworkSafetyError as error:
                network_errors.append(str(error))
                continue
            parser = _LinkParser()
            parser.feed(fetched.body.decode("utf-8", errors="replace"))
            for href, title, attrs in parser.links:
                css_class = attrs.get("class", "").casefold()
                if not title or not (
                    "viewselectedactdetailsservlet" in href.casefold()
                    or "allacts" in css_class
                ):
                    continue
                landing_url = _official_urljoin(fetched.final_url, href)
                identity_key = title.casefold().strip().rstrip(".")
                if identity_key in seen:
                    continue
                seen.add(identity_key)
                match = verify_identity(reference, discovered_title=title)
                candidates.append(
                    SourceCandidate(
                        target_key=reference.target_key,
                        source_adapter=self.definition.source_id,
                        source_domain=urllib.parse.urlparse(landing_url).hostname or "",
                        source_authority=self.definition.authority_class.value,
                        discovery_query=query,
                        discovery_status="DISCOVERED",
                        discovery_url=url,
                        landing_url=landing_url,
                        redirect_chain=fetched.redirect_chain,
                        discovered_title=title,
                        discovered_identifier=match.discovered_identifier,
                        discovered_document_kind=reference.reference_kind,
                        identity_match_status=match.status,
                        identity_evidence=match.evidence,
                        acquisition_status="DISCOVERED",
                        target_state=(
                            TargetState.IDENTITY_MISMATCH.value
                            if match.status == "MISMATCH"
                            else TargetState.AMBIGUOUS.value
                            if match.status == "AMBIGUOUS"
                            else ""
                        ),
                        raw_source_evidence={
                            "search_url": url,
                            "search_final_url": fetched.final_url,
                            "search_from_cache": fetched.from_cache,
                        },
                    )
                )
                if len(candidates) >= 5:
                    return candidates
            if candidates:
                return candidates
        if network_errors:
            return [
                SourceCandidate(
                    target_key=reference.target_key,
                    source_adapter=self.definition.source_id,
                    source_domain=self.definition.base_host,
                    source_authority=self.definition.authority_class.value,
                    discovery_query=(self.build_queries(reference) or [reference.normalized_identifier])[0],
                    discovery_status="NETWORK_FAILED",
                    acquisition_status="BLOCKED",
                    verification_status="BLOCKED",
                    target_state=TargetState.BLOCKED_BY_SOURCE.value,
                    raw_source_evidence={"reason": "SOURCE_UNAVAILABLE", "errors": network_errors},
                )
            ]
        return [
            SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host,
                source_authority=self.definition.authority_class.value,
                discovery_query=(self.build_queries(reference) or [reference.normalized_identifier])[0],
                discovery_status="NOT_FOUND",
                acquisition_status="NOT_FOUND",
                target_state=TargetState.NOT_FOUND_AUTHORITATIVE.value,
                raw_source_evidence={"queries_attempted": self.build_queries(reference)},
            )
        ]

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate:
        if candidate.discovery_status != "DISCOVERED" or not candidate.landing_url:
            return candidate
        fetched = self.client.fetch(candidate.landing_url)
        parser = _LinkParser()
        parser.feed(fetched.body.decode("utf-8", errors="replace"))
        bitstreams: list[tuple[int, str, str]] = []
        for href, title, _ in parser.links:
            clean_href = html.unescape(href).strip()
            if "/bitstream/" not in clean_href.casefold() or not clean_href.casefold().endswith(".pdf"):
                continue
            url = _official_urljoin(fetched.final_url, clean_href)
            title_match = int(title.casefold().rstrip(".") == candidate.discovered_title.casefold().rstrip("."))
            bitstreams.append((title_match, url, title))
        if not bitstreams:
            return replace(
                candidate,
                landing_url=fetched.final_url,
                redirect_chain=tuple(dict.fromkeys(candidate.redirect_chain + fetched.redirect_chain)),
                acquisition_status="DOWNLOAD_FAILED",
                target_state=TargetState.DOWNLOAD_FAILED.value,
                raw_source_evidence={
                    **candidate.raw_source_evidence,
                    "metadata_error": "No official primary /bitstream/ PDF link found",
                },
            )
        bitstreams.sort(key=lambda row: (-row[0], row[1]))
        _, document_url, link_title = bitstreams[0]
        return replace(
            candidate,
            landing_url=fetched.final_url,
            document_url=document_url,
            redirect_chain=tuple(dict.fromkeys(candidate.redirect_chain + fetched.redirect_chain)),
            raw_source_evidence={
                **candidate.raw_source_evidence,
                "landing_from_cache": fetched.from_cache,
                "primary_pdf_link_title": link_title,
                "official_pdf_links_found": len(bitstreams),
            },
        )

    def download(self, candidate: SourceCandidate) -> tuple[FetchResult, PdfProbe]:
        if not candidate.document_url:
            raise NetworkSafetyError("Candidate has no official document URL")
        fetched = self.client.fetch(candidate.document_url, expected_pdf=True)
        return fetched, validate_pdf(fetched.body)

    def verify_authority(self, candidate: SourceCandidate) -> bool:
        allowed = {item.casefold() for item in self.definition.domains}
        urls = [candidate.discovery_url, candidate.landing_url, candidate.document_url]
        for url in (item for item in urls if item):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in self.definition.allowed_url_schemes:
                return False
            if (parsed.hostname or "").casefold() not in allowed:
                return False
        return candidate.source_authority == self.definition.authority_class.value


class _OfficialIndexParser(HTMLParser):
    """Extract PDF link, title and date from ordinary table rows."""

    def __init__(self) -> None:
        super().__init__()
        self._row_depth = 0
        self._texts: list[str] = []
        self._links: list[str] = []
        self.rows: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._texts, self._links = [], []
        if self._row_depth and tag.casefold() == "a":
            href = dict(attrs).get("href", "")
            if href and ".pdf" in href.casefold():
                self._links.append(href)

    def handle_data(self, data):
        if self._row_depth:
            clean = " ".join(data.split())
            if clean:
                self._texts.append(clean)

    def handle_endtag(self, tag):
        if tag.casefold() != "tr" or not self._row_depth:
            return
        self._row_depth -= 1
        if self._row_depth:
            return
        text = " ".join(self._texts)
        date_match = re.search(r"\b(\d{2})[-/](\d{2})[-/](\d{4})\b", text)
        date_value = f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}" if date_match else ""
        for href in self._links:
            self.rows.append((href, text, date_value))


class DivyangSahayakSource:
    """Official secondary, public index for disability-related Maharashtra GRs."""

    INDEX_URL = "https://divyangsahayakportal.maharashtra.gov.in/site/government-resolutions"

    def __init__(self, definition: SourceDefinition, *, cache_dir: Path):
        self.definition = definition
        policy = definition.rate_policy
        self.client = SafeHttpClient(
            allowed_domains=definition.domains, allowed_schemes=definition.allowed_url_schemes,
            cache_dir=cache_dir, timeout_seconds=settings.external_http_timeout_seconds or policy.timeout_seconds,
            max_bytes=settings.external_http_max_bytes, max_redirects=settings.external_http_max_redirects,
            retry_count=settings.external_http_retry_count, retry_backoff_seconds=policy.backoff_seconds,
            rate_delay_seconds=max(settings.external_http_rate_delay_seconds, policy.request_delay_seconds),
            user_agent=settings.external_http_user_agent,
        )

    def supports(self, reference: ReferenceIdentity) -> bool:
        issuer = reference.issuer.casefold()
        return (
            reference.reference_kind in self.definition.supported_document_kinds
            and ("disabil" in issuer or "divyang" in issuer or reference.normalized_identifier.startswith("इडीडी"))
        )

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        return generate_queries(reference)

    @staticmethod
    def _probe_contains(reference: ReferenceIdentity, probe_text: str) -> str:
        expected = normalize_document_number(reference.normalized_identifier)
        for raw_line in probe_text.splitlines()[:80]:
            normalized = normalize_document_number(raw_line)
            if normalized == expected:
                return normalized
            if expected.endswith(("/", "-")) and normalized.startswith(expected):
                return normalized
        return ""

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        if not self.supports(reference):
            return [SourceCandidate(
                target_key=reference.target_key, source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host, source_authority=self.definition.authority_class.value,
                discovery_query=reference.normalized_identifier, discovery_status="NOT_FOUND",
                acquisition_status="NOT_FOUND", target_state=TargetState.NOT_FOUND_AUTHORITATIVE.value,
                raw_source_evidence={"reason": "SOURCE_SCOPE_NOT_SUPPORTED"},
            )]
        if not reference.reference_date:
            return [SourceCandidate(
                target_key=reference.target_key, source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host, source_authority=self.definition.authority_class.value,
                discovery_query=reference.normalized_identifier, discovery_status="NOT_FOUND",
                acquisition_status="NOT_FOUND", target_state=TargetState.NOT_FOUND_AUTHORITATIVE.value,
                raw_source_evidence={"reason": "OFFICIAL_INDEX_DATE_REQUIRED"},
            )]
        try:
            fetched = self.client.fetch(self.INDEX_URL)
            parser = _OfficialIndexParser()
            parser.feed(fetched.body.decode("utf-8", errors="replace"))
            date_value = reference.reference_date.isoformat()
            matched_rows = [row for row in parser.rows if row[2] == date_value][:8]
            candidates = []
            for href, title, listed_date in matched_rows:
                url = _official_urljoin(fetched.final_url, href)
                candidates.append(SourceCandidate(
                    target_key=reference.target_key, source_adapter=self.definition.source_id,
                    source_domain=urllib.parse.urlparse(url).hostname or "",
                    source_authority=self.definition.authority_class.value,
                    discovery_query=reference.normalized_identifier, discovery_status="DISCOVERED",
                    discovery_url=self.INDEX_URL, landing_url=self.INDEX_URL, document_url=url,
                    redirect_chain=fetched.redirect_chain,
                    discovered_title=title, discovered_identifier="",
                    discovered_date=reference.reference_date, discovered_issuer=reference.issuer,
                    discovered_document_kind=reference.reference_kind,
                    identity_match_status="NOT_EVALUATED", identity_evidence={},
                    acquisition_status="DISCOVERED",
                    raw_source_evidence={
                        "official_index_date": listed_date,
                        "official_index_title": title,
                        "identity_deferred_to_local_pdf": True,
                    },
                ))
            if candidates:
                return candidates
            reason = "TARGET_NOT_PUBLISHED_ON_SOURCE" if matched_rows else "SOURCE_INDEX_INCOMPLETE"
        except NetworkSafetyError as error:
            reason = "ALTERNATE_OFFICIAL_SOURCE_UNAVAILABLE"
            detail = str(error)
        else:
            detail = ""
        return [SourceCandidate(
            target_key=reference.target_key, source_adapter=self.definition.source_id,
            source_domain=self.definition.base_host, source_authority=self.definition.authority_class.value,
            discovery_query=reference.normalized_identifier, discovery_status="NOT_FOUND",
            acquisition_status="NOT_FOUND", target_state=TargetState.NOT_FOUND_AUTHORITATIVE.value,
            raw_source_evidence={"reason": reason, "network_error": detail},
        )]

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate:
        return candidate

    def download(self, candidate: SourceCandidate) -> tuple[FetchResult, PdfProbe]:
        fetched = self.client.fetch(candidate.document_url, expected_pdf=True)
        return fetched, validate_pdf(fetched.body)

    def verify_authority(self, candidate: SourceCandidate) -> bool:
        allowed = {domain.casefold() for domain in self.definition.domains}
        return all(
            urllib.parse.urlparse(url).scheme == "https"
            and (urllib.parse.urlparse(url).hostname or "").casefold() in allowed
            for url in (candidate.discovery_url, candidate.landing_url, candidate.document_url) if url
        ) and candidate.source_authority == self.definition.authority_class.value


class MaharashtraDepartmentSource:
    """Reusable exact-query adapter for official Maharashtra department sites."""

    DEPARTMENT_SCOPE = {
        "maharashtra_finance": "finance department",
        "maharashtra_parliamentary_affairs": "parliamentary affairs department",
        "maharashtra_environment": "environment department",
        "maharashtra_marathi_language": "marathi language department",
        "maharashtra_it": "electronics, information technology and artificial intelligence department",
    }

    def __init__(self, definition: SourceDefinition, *, cache_dir: Path):
        self.definition = definition
        policy = definition.rate_policy
        self.client = SafeHttpClient(
            allowed_domains=definition.domains,
            allowed_schemes=definition.allowed_url_schemes,
            cache_dir=cache_dir,
            timeout_seconds=settings.external_http_timeout_seconds or policy.timeout_seconds,
            max_bytes=settings.external_http_max_bytes,
            max_redirects=settings.external_http_max_redirects,
            retry_count=settings.external_http_retry_count,
            retry_backoff_seconds=policy.backoff_seconds,
            rate_delay_seconds=max(
                settings.external_http_rate_delay_seconds, policy.request_delay_seconds
            ),
            user_agent=settings.external_http_user_agent,
        )

    def supports(self, reference: ReferenceIdentity) -> bool:
        expected = self.DEPARTMENT_SCOPE.get(self.definition.source_id, "")
        return (
            reference.reference_kind in self.definition.supported_document_kinds
            and bool(expected)
            and reference.issuer.strip().casefold() == expected
        )

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        return generate_queries(reference)

    def _search_url(self, reference: ReferenceIdentity) -> str:
        query = urllib.parse.quote(reference.normalized_identifier, safe="")
        if self.definition.source_id == "maharashtra_it":
            return f"https://{self.definition.base_host}/search/node?keys={query}"
        return f"https://{self.definition.base_host}/?s={query}"

    def _links(self, body: bytes, base_url: str) -> list[tuple[str, str]]:
        parser = _LinkParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        allowed = {item.casefold() for item in self.definition.domains}
        results: list[tuple[str, str]] = []
        for href, title, _attrs in parser.links:
            url = _official_urljoin(base_url, href)
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed:
                continue
            path = parsed.path.casefold()
            is_pdf = path.endswith(".pdf")
            is_document = "/document/" in path and "/document-category/" not in path
            is_marathi_post = (
                self.definition.source_id == "maharashtra_marathi_language"
                and bool(re.fullmatch(r"/\d+/?", parsed.path))
            )
            if is_pdf or is_document or is_marathi_post:
                item = (url, title)
                if item not in results:
                    results.append(item)
            if len(results) >= 8:
                break
        return results

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        if not self.supports(reference):
            return []
        search_url = self._search_url(reference)
        try:
            fetched = self.client.fetch(search_url)
            links = self._links(fetched.body, fetched.final_url)
        except NetworkSafetyError as error:
            return [SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.source_id,
                source_domain=self.definition.base_host,
                source_authority=self.definition.authority_class.value,
                discovery_query=reference.normalized_identifier,
                discovery_status="NETWORK_FAILED",
                acquisition_status="BLOCKED",
                target_state=TargetState.NETWORK_FAILED.value,
                raw_source_evidence={
                    "reason": "OFFICIAL_DEPARTMENT_SOURCE_UNAVAILABLE",
                    "network_error": str(error),
                },
            )]
        candidates = []
        for url, title in links:
            direct_pdf = urllib.parse.urlparse(url).path.casefold().endswith(".pdf")
            candidates.append(SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.source_id,
                source_domain=urllib.parse.urlparse(url).hostname or "",
                source_authority=self.definition.authority_class.value,
                discovery_query=reference.normalized_identifier,
                discovery_status="DISCOVERED",
                discovery_url=search_url,
                landing_url=fetched.final_url if direct_pdf else url,
                document_url=url if direct_pdf else "",
                redirect_chain=fetched.redirect_chain,
                discovered_title=title,
                discovered_issuer=reference.issuer,
                discovered_document_kind=reference.reference_kind,
                identity_match_status="NOT_EVALUATED",
                acquisition_status="DISCOVERED",
                raw_source_evidence={
                    "identity_deferred_to_local_pdf": True,
                    "official_department_search": fetched.final_url,
                },
            ))
        if candidates:
            return candidates
        return [SourceCandidate(
            target_key=reference.target_key,
            source_adapter=self.definition.source_id,
            source_domain=self.definition.base_host,
            source_authority=self.definition.authority_class.value,
            discovery_query=reference.normalized_identifier,
            discovery_status="NOT_FOUND",
            acquisition_status="NOT_FOUND",
            target_state=TargetState.NOT_FOUND_AUTHORITATIVE.value,
            raw_source_evidence={"reason": "TARGET_NOT_PUBLISHED_ON_DEPARTMENT_SOURCE"},
        )]

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate:
        if candidate.document_url:
            return candidate
        try:
            fetched = self.client.fetch(candidate.landing_url)
            links = [item for item in self._links(fetched.body, fetched.final_url) if item[0].casefold().endswith(".pdf")]
        except NetworkSafetyError as error:
            return replace(
                candidate,
                acquisition_status="DOWNLOAD_FAILED",
                raw_source_evidence={**candidate.raw_source_evidence, "metadata_error": str(error)},
            )
        if not links:
            return replace(
                candidate,
                acquisition_status="DOWNLOAD_FAILED",
                raw_source_evidence={**candidate.raw_source_evidence, "metadata_error": "NO_OFFICIAL_PDF_LINK"},
            )
        return replace(
            candidate,
            landing_url=fetched.final_url,
            document_url=links[0][0],
            redirect_chain=tuple(dict.fromkeys(candidate.redirect_chain + fetched.redirect_chain)),
        )

    def download(self, candidate: SourceCandidate) -> tuple[FetchResult, PdfProbe]:
        if not candidate.document_url:
            raise NetworkSafetyError("Candidate has no official PDF URL")
        fetched = self.client.fetch(candidate.document_url, expected_pdf=True)
        return fetched, validate_pdf(fetched.body)

    def verify_authority(self, candidate: SourceCandidate) -> bool:
        allowed = {domain.casefold() for domain in self.definition.domains}
        return all(
            urllib.parse.urlparse(url).scheme == "https"
            and (urllib.parse.urlparse(url).hostname or "").casefold() in allowed
            for url in (candidate.discovery_url, candidate.landing_url, candidate.document_url)
            if url
        ) and candidate.source_authority == self.definition.authority_class.value


__all__ = [
    "BlockedOfficialSource", "DivyangSahayakSource", "ExternalDocumentSource", "IndiaCodeSource",
    "MaharashtraDepartmentSource",
    "_LinkParser", "_OfficialIndexParser", "_official_urljoin",
]
