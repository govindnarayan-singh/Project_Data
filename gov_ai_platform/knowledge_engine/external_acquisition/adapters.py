from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from knowledge_engine.settings import settings
from knowledge_engine.graph.normalization import normalize_document_number, normalize_legal_reference

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
    SEARCH_ROOT = "https://indiacode.gov.in/server/api/discover/search/objects"

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
            state_named = bool(re.search(r"\b(?:MAHARASHTRA|BOMBAY)\b", reference.normalized_identifier))
            collection = (
                "ACT_AMENDMENT"
                if reference.reference_kind == "ACT" and "_AMENDMENT_ACT_" in reference.normalized_identifier and not state_named
                else "ACT" if reference.reference_kind == "ACT" else "RULE"
            )
            url = self.SEARCH_ROOT + "?" + urllib.parse.urlencode({
                "query": query,
                "size": 100,
                "dsoType": "ITEM",
                "f.identifier_collection": f"{collection},equals",
                "embed": ["objects", "objects.indexableObject"],
            }, doseq=True)
            try:
                fetched = self.client.fetch(url)
            except NetworkSafetyError as error:
                network_errors.append(str(error))
                continue
            try:
                payload = json.loads(fetched.body)
                objects = payload.get("_embedded", {}).get("searchResult", {}).get("_embedded", {}).get("objects", [])
            except (TypeError, ValueError) as error:
                network_errors.append(f"Official India Code search response was not JSON: {error}")
                continue
            expected_jurisdiction = "MAHARASHTRA" if state_named else "CENTRAL"
            for result in objects:
                item = result.get("_embedded", {}).get("indexableObject", {})
                title = str(item.get("name") or "").strip()
                metadata = item.get("metadata") or {}
                state_values = {
                    str(value.get("value") or "").strip().upper()
                    for value in metadata.get("dc.identifier.state_name", [])
                }
                collection_values = {
                    str(value.get("value") or "").strip().upper()
                    for value in metadata.get("dc.identifier.collection", [])
                }
                item_id = str(item.get("uuid") or item.get("id") or "").strip()
                if not title or not item_id or collection not in collection_values:
                    continue
                match = verify_identity(reference, discovered_title=title)
                if match.status not in {"MATCH_EXACT", "MATCH_STRONG", "MATCH_EXACT_VERSION"}:
                    continue
                central_amendment_collection = (
                    expected_jurisdiction == "CENTRAL"
                    and collection == "ACT_AMENDMENT"
                    and not state_values
                )
                if expected_jurisdiction not in state_values and not central_amendment_collection:
                    continue
                landing_url = f"https://indiacode.gov.in/server/api/core/items/{item_id}"
                identity_key = f"{title.casefold().strip().rstrip('.')}|{expected_jurisdiction}"
                if identity_key in seen:
                    continue
                seen.add(identity_key)
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
                            "official_item_uuid": item_id,
                            "official_handle": item.get("handle", ""),
                            "official_jurisdiction": sorted(state_values),
                            "official_collection": sorted(collection_values),
                            "api_version": "DSpace_9.1",
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
        try:
            item = json.loads(fetched.body)
            bundles_url = item.get("_links", {}).get("bundles", {}).get("href", "")
            if not bundles_url:
                raise ValueError("item has no bundles link")
            bundle_response = self.client.fetch(bundles_url + "?size=20")
            bundles = json.loads(bundle_response.body).get("_embedded", {}).get("bundles", [])
            bitstreams: list[tuple[int, int, str, str]] = []
            for bundle in bundles:
                if str(bundle.get("name") or "").upper() != "ORIGINAL":
                    continue
                bits_url = bundle.get("_links", {}).get("bitstreams", {}).get("href", "")
                if not bits_url:
                    continue
                bits_response = self.client.fetch(bits_url + "?size=20")
                bits = json.loads(bits_response.body).get("_embedded", {}).get("bitstreams", [])
                for bitstream in bits:
                    name = str(bitstream.get("name") or "")
                    content_url = bitstream.get("_links", {}).get("content", {}).get("href", "")
                    if content_url and name.casefold().endswith(".pdf"):
                        # India Code's conventional ORIGINAL names use an
                        # `aYYYY-` prefix for the English Act and `h...` for
                        # Hindi.  Prefer English because legal identity
                        # matching is against the frozen English target.
                        english_name = int(bool(re.match(r"(?i)^a(?:18|19|20)\d{2}[-_]", name)))
                        bitstreams.append((english_name, int(bitstream.get("sizeBytes") or 0), content_url, name))
        except (NetworkSafetyError, TypeError, ValueError) as error:
            bitstreams = []
            metadata_error = str(error)
        else:
            metadata_error = ""
        if not bitstreams:
            return replace(
                candidate,
                landing_url=fetched.final_url,
                redirect_chain=tuple(dict.fromkeys(candidate.redirect_chain + fetched.redirect_chain)),
                acquisition_status="DOWNLOAD_FAILED",
                target_state=TargetState.DOWNLOAD_FAILED.value,
                raw_source_evidence={
                    **candidate.raw_source_evidence,
                    "metadata_error": metadata_error or "No PDF found in the official ORIGINAL bundle",
                },
            )
        bitstreams.sort(key=lambda row: (-row[0], -row[1], row[2]))
        _, _, document_url, link_title = bitstreams[0]
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
                "official_bundle": "ORIGINAL",
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
        "maharashtra_law_judiciary": "law and judiciary department",
        "maharashtra_social_justice": "persons with disabilities welfare department",
        "maharashtra_minority_development": "minorities development department",
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
        legal_open_scope = (
            self.definition.source_id == "maharashtra_law_judiciary"
            and reference.reference_kind in {"ACT", "RULE"}
            and not reference.issuer.strip()
            and (
                "maharashtra" in f"{reference.normalized_identifier} {reference.raw_identifier}".casefold()
                or "महाराष्ट्र" in f"{reference.normalized_identifier} {reference.raw_identifier}".casefold()
            )
        )
        return legal_open_scope or (
            reference.reference_kind in self.definition.supported_document_kinds
            and bool(expected)
            and reference.issuer.strip().casefold() == expected
        )

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        return generate_queries(reference)

    def _search_url(self, query_text: str) -> str:
        query = urllib.parse.quote(query_text, safe="")
        if self.definition.source_id == "maharashtra_it":
            return f"https://{self.definition.base_host}/search/node?keys={query}"
        if self.definition.listing_strategy.startswith("PUBLIC_S3WAAS"):
            return f"https://{self.definition.base_host}/en/?s={query}"
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
        candidates = []
        network_errors: list[str] = []
        for query_text in self.build_queries(reference):
            search_url = self._search_url(query_text)
            try:
                fetched = self.client.fetch(search_url)
                links = self._links(fetched.body, fetched.final_url)
            except NetworkSafetyError as error:
                network_errors.append(str(error))
                continue
            for url, title in links:
                direct_pdf = urllib.parse.urlparse(url).path.casefold().endswith(".pdf")
                candidate = SourceCandidate(
                    target_key=reference.target_key,
                    source_adapter=self.definition.source_id,
                    source_domain=urllib.parse.urlparse(url).hostname or "",
                    source_authority=self.definition.authority_class.value,
                    discovery_query=query_text,
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
                )
                if not any(item.document_url == candidate.document_url and item.landing_url == candidate.landing_url for item in candidates):
                    candidates.append(candidate)
            if candidates:
                break
        if network_errors and not candidates:
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
                    "network_error": "; ".join(network_errors),
                },
            )]
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


_S3WAAS_LISTING_CACHE: dict[str, list[tuple[str, str, int]]] = {}


class MaharashtraS3WaaSDocumentSource(MaharashtraDepartmentSource):
    """Reusable bounded reader for public Maharashtra S3WaaS document indexes.

    Discovery uses only public HTML pagination and official PDF links.  Titles
    are a coarse discovery filter; trust remains deferred to the locally
    persisted Incoming PDF gate in the acquisition service.
    """

    _GENERIC_LEGAL_TOKENS = {
        "THE", "MAHARASHTRA", "BOMBAY", "ACT", "OF", "AND", "TO",
        "AMENDMENT",
    }

    @staticmethod
    def _page_url(listing_url: str, page: int) -> str:
        root = listing_url.rstrip("/") + "/"
        return root if page == 1 else urllib.parse.urljoin(root, f"page/{page}/")

    def build_queries(self, reference: ReferenceIdentity) -> list[str]:
        queries = super().build_queries(reference)
        if reference.reference_kind in {"ACT", "RULE"}:
            for query in list(queries):
                without_state = re.sub(r"\b(?:THE\s+)?MAHARASHTRA\s+", "", query, flags=re.IGNORECASE)
                if without_state and without_state not in queries:
                    queries.append(without_state)
            # The official Law and Judiciary archive contains an observed
            # "Parishands" index-title spelling.  This is a source-level
            # search alias only; local PDF identity still requires PARISHADS.
            # Prefer the archive's unprefixed observed title inside the bounded
            # query budget.  The canonical identity is still attempted first.
            for query in reversed(list(queries)):
                observed_spelling = re.sub(r"\bPARISHADS\b", "PARISHANDS", query, flags=re.IGNORECASE)
                if observed_spelling and observed_spelling not in queries:
                    queries.append(observed_spelling)
        return queries[:3]

    @classmethod
    def _legal_title_score(cls, expected: str, title: str, kind: str) -> float:
        if kind == "ACT" and not re.search(r"\bact\b", title, re.IGNORECASE):
            return 0.0
        if kind == "RULE" and not re.search(r"\brules?\b", title, re.IGNORECASE):
            return 0.0
        discovered = normalize_legal_reference(title, kind)
        expected = expected.removeprefix("THE_")
        discovered = discovered.removeprefix("THE_")
        expected_year = re.search(r"_(?:19|20)\d{2}$", expected)
        discovered_year = re.search(r"_(?:19|20)\d{2}$", discovered)
        if not expected_year or not discovered_year or expected_year.group() != discovered_year.group():
            return 0.0
        expected_tokens = {
            token for token in expected[:expected_year.start()].split("_")
            if token and token not in cls._GENERIC_LEGAL_TOKENS
        }
        discovered_tokens = {
            token for token in discovered[:discovered_year.start()].split("_")
            if token and token not in cls._GENERIC_LEGAL_TOKENS
        }
        for ordinal in ("SECOND", "THIRD"):
            if (ordinal in expected_tokens) != (ordinal in discovered_tokens):
                return 0.0
        if "AMENDMENT" in expected and "AMENDMENT" not in discovered:
            return 0.0
        if not expected_tokens:
            return 0.0
        overlap = len(expected_tokens & discovered_tokens) / len(expected_tokens)
        return overlap if overlap >= 0.75 else 0.0

    def _listing_entries(self) -> tuple[list[tuple[str, str, int]], list[str]]:
        cached = _S3WAAS_LISTING_CACHE.get(self.definition.source_id)
        if cached is not None:
            return cached, []
        listing_url = self.definition.listing_url
        max_pages = self.definition.listing_max_pages
        if not listing_url or max_pages < 1:
            return [], []
        entries: list[tuple[str, str, int]] = []
        errors: list[str] = []
        first_body: bytes | None = None
        first_final = listing_url
        try:
            first = self.client.fetch(self._page_url(listing_url, 1))
            first_body, first_final = first.body, first.final_url
            links = _LinkParser()
            links.feed(first.body.decode("utf-8", errors="replace"))
            observed_pages = [
                int(match.group(1))
                for href, _title, _attrs in links.links
                if (match := re.search(r"/page/(\d+)/?", href))
            ]
            page_limit = min(max_pages, max(observed_pages, default=1))
        except NetworkSafetyError as error:
            return [], [str(error)]
        for page in range(1, page_limit + 1):
            try:
                if page == 1 and first_body is not None:
                    body, final_url = first_body, first_final
                else:
                    fetched = self.client.fetch(self._page_url(listing_url, page))
                    body, final_url = fetched.body, fetched.final_url
                parser = _OfficialIndexParser()
                parser.feed(body.decode("utf-8", errors="replace"))
                for href, title, _date in parser.rows:
                    url = _official_urljoin(final_url, href)
                    if (url, title, page) not in entries:
                        entries.append((url, title, page))
            except NetworkSafetyError as error:
                errors.append(f"page {page}: {error}")
        _S3WAAS_LISTING_CACHE[self.definition.source_id] = entries
        return entries, errors

    def search(self, reference: ReferenceIdentity) -> list[SourceCandidate]:
        if not self.supports(reference):
            return []
        if reference.reference_kind not in {"ACT", "RULE"}:
            return super().search(reference)
        expected = reference.normalized_identifier
        entries: list[tuple[str, str, int]] = []
        errors: list[str] = []
        for query in self.build_queries(reference):
            search_url = self._search_url(query)
            try:
                fetched = self.client.fetch(search_url)
                entries.extend((url, title, 0) for url, title in self._links(fetched.body, fetched.final_url))
            except NetworkSafetyError as error:
                errors.append(str(error))
            if entries:
                break
        ranked = sorted(
            (
                (self._legal_title_score(expected, title, reference.reference_kind), url, title, page)
                for url, title, page in entries
            ),
            key=lambda item: (-item[0], item[3], item[1]),
        )
        candidates: list[SourceCandidate] = []
        for score, url, title, page in ranked:
            if score <= 0:
                break
            candidates.append(SourceCandidate(
                target_key=reference.target_key,
                source_adapter=self.definition.source_id,
                source_domain=urllib.parse.urlparse(url).hostname or "",
                source_authority=self.definition.authority_class.value,
                discovery_query=(self.build_queries(reference) or [expected])[0],
                discovery_status="DISCOVERED",
                discovery_url=self._search_url((self.build_queries(reference) or [expected])[0]),
                landing_url=(self._search_url((self.build_queries(reference) or [expected])[0])
                             if urllib.parse.urlparse(url).path.casefold().endswith(".pdf") else url),
                document_url=url,
                discovered_title=title,
                discovered_issuer=reference.issuer,
                discovered_document_kind=reference.reference_kind,
                identity_match_status="NOT_EVALUATED",
                acquisition_status="DISCOVERED",
                raw_source_evidence={
                    "identity_deferred_to_local_pdf": True,
                    "listing_strategy": self.definition.listing_strategy,
                    "listing_page": page or "OFFICIAL_SEARCH",
                    "discovery_title_score": score,
                },
            ))
            if len(candidates) >= 5:
                break
        if candidates:
            return candidates
        status = "NETWORK_FAILED" if errors and not entries else "NOT_FOUND"
        return [SourceCandidate(
            target_key=reference.target_key,
            source_adapter=self.definition.source_id,
            source_domain=self.definition.base_host,
            source_authority=self.definition.authority_class.value,
            discovery_query=(self.build_queries(reference) or [expected])[0],
            discovery_status=status,
            acquisition_status="BLOCKED" if status == "NETWORK_FAILED" else "NOT_FOUND",
            verification_status="BLOCKED" if status == "NETWORK_FAILED" else "UNVERIFIED",
            target_state=(TargetState.NETWORK_FAILED.value if status == "NETWORK_FAILED"
                          else TargetState.NOT_FOUND_AUTHORITATIVE.value),
            raw_source_evidence={
                "reason": "OFFICIAL_PUBLIC_INDEX_NO_EXACT_TITLE",
                "listing_entries_examined": len(entries),
                "network_errors": errors,
            },
        )]


__all__ = [
    "BlockedOfficialSource", "DivyangSahayakSource", "ExternalDocumentSource", "IndiaCodeSource",
    "MaharashtraDepartmentSource", "MaharashtraS3WaaSDocumentSource",
    "_LinkParser", "_OfficialIndexParser", "_official_urljoin",
]
