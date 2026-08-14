from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class NetworkSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    content_type: str
    body: bytes
    status_code: int = 200
    redirect_chain: tuple[str, ...] = ()
    from_cache: bool = False


def _unsafe_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not address.is_global


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(
        self,
        allowed_domains: set[str],
        maximum: int,
        validator: Callable[[str], None] | None = None,
    ):
        self.allowed_domains = {item.casefold() for item in allowed_domains}
        self.maximum = maximum
        self.validator = validator
        self.redirects: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if len(self.redirects) >= self.maximum:
            raise NetworkSafetyError("Redirect limit exceeded")
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in self.allowed_domains:
            raise NetworkSafetyError("Redirect left the configured HTTPS official-source boundary")
        if self.validator:
            self.validator(newurl)
        self.redirects.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHttpClient:
    def __init__(
        self,
        *,
        allowed_domains: tuple[str, ...],
        cache_dir: Path,
        allowed_schemes: tuple[str, ...] = ("https",),
        timeout_seconds: float = 15.0,
        max_bytes: int = 25 * 1024 * 1024,
        max_redirects: int = 3,
        retry_count: int = 1,
        retry_backoff_seconds: float = 0.75,
        rate_delay_seconds: float = 0.5,
        user_agent: str = "GovAIAdministrativeAcquisition/1.1 (+official public documents)",
    ):
        self.allowed_domains = {domain.casefold() for domain in allowed_domains}
        self.allowed_schemes = {scheme.casefold() for scheme in allowed_schemes}
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds
        self.rate_delay_seconds = rate_delay_seconds
        self.user_agent = user_agent
        self._last_request = 0.0

    def _validate_url(self, url: str, *, resolve_dns: bool = False) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.casefold() not in self.allowed_schemes:
            raise NetworkSafetyError("External URL scheme is not permitted")
        if parsed.username or parsed.password:
            raise NetworkSafetyError("Credentials in external URLs are forbidden")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if not host or host not in self.allowed_domains:
            raise NetworkSafetyError("URL domain is not in the configured source adapter allowlist")
        if host == "localhost" or host.endswith(".localhost") or ".local" == host[-6:]:
            raise NetworkSafetyError("Internal hostnames are forbidden")
        try:
            if _unsafe_ip(host):
                raise NetworkSafetyError("Private, loopback, link-local, or reserved IP is forbidden")
        except ValueError:
            pass
        if resolve_dns:
            try:
                addresses = {
                    item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
                }
            except OSError as error:
                raise NetworkSafetyError(f"Official host DNS resolution failed: {error}") from error
            if not addresses or any(_unsafe_ip(address) for address in addresses):
                raise NetworkSafetyError("Official hostname resolved to an unsafe network address")

    def fetch(self, url: str, *, expected_pdf: bool = False) -> FetchResult:
        self._validate_url(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.bin"
        meta_path = self.cache_dir / f"{cache_key}.json"
        if body_path.is_file() and meta_path.is_file():
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes()
            self._validate_url(metadata["final_url"])
            self._validate_payload(body, metadata["content_type"], expected_pdf)
            return FetchResult(
                url,
                metadata["final_url"],
                metadata["content_type"],
                body,
                int(metadata.get("status_code", 200)),
                tuple(metadata.get("redirect_chain", [])),
                True,
            )

        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                self._validate_url(url, resolve_dns=True)
                remaining = self.rate_delay_seconds - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
                redirect = _BoundedRedirectHandler(
                    self.allowed_domains,
                    self.max_redirects,
                    lambda value: self._validate_url(value, resolve_dns=True),
                )
                opener = urllib.request.build_opener(redirect)
                request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                self._last_request = time.monotonic()
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    final_url = response.geturl()
                    self._validate_url(final_url, resolve_dns=True)
                    status_code = int(response.getcode() or 0)
                    if not 200 <= status_code < 300:
                        raise NetworkSafetyError(f"Official source returned HTTP {status_code}")
                    content_type = response.headers.get_content_type().casefold()
                    length = response.headers.get("Content-Length")
                    if length and int(length) > self.max_bytes:
                        raise NetworkSafetyError("Response exceeds the configured maximum size")
                    body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise NetworkSafetyError("Response exceeds the configured maximum size")
                self._validate_payload(body, content_type, expected_pdf)
                metadata = {
                    "final_url": final_url,
                    "content_type": content_type,
                    "status_code": status_code,
                    "redirect_chain": redirect.redirects,
                }
                temporary_body = Path(str(body_path) + ".tmp")
                temporary_meta = Path(str(meta_path) + ".tmp")
                temporary_body.write_bytes(body)
                temporary_meta.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
                temporary_body.replace(body_path)
                temporary_meta.replace(meta_path)
                return FetchResult(
                    url, final_url, content_type, body, status_code, tuple(redirect.redirects), False
                )
            except (OSError, urllib.error.URLError, NetworkSafetyError, ValueError, http.client.HTTPException) as error:
                last_error = error
                if attempt >= self.retry_count:
                    break
                if self.retry_backoff_seconds > 0:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
        raise NetworkSafetyError(str(last_error or "External request failed"))

    @staticmethod
    def _validate_payload(body: bytes, content_type: str, expected_pdf: bool) -> None:
        if not body:
            raise NetworkSafetyError("Official source returned an empty response")
        if expected_pdf and (content_type != "application/pdf" or not body.startswith(b"%PDF")):
            raise NetworkSafetyError("Expected a PDF but received HTML, an error payload, or an invalid signature")


__all__ = ["FetchResult", "NetworkSafetyError", "SafeHttpClient", "_BoundedRedirectHandler"]
