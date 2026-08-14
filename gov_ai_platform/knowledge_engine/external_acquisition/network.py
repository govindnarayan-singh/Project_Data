from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class NetworkSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    content_type: str
    body: bytes


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_domains: set[str], maximum: int):
        self.allowed_domains = allowed_domains
        self.maximum = maximum
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > self.maximum:
            raise NetworkSafetyError("Redirect limit exceeded")
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in self.allowed_domains:
            raise NetworkSafetyError("Redirect left the configured HTTPS official-source boundary")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHttpClient:
    def __init__(
        self,
        *,
        allowed_domains: tuple[str, ...],
        cache_dir: Path,
        timeout_seconds: float = 15.0,
        max_bytes: int = 25 * 1024 * 1024,
        max_redirects: int = 3,
        retry_count: int = 1,
        rate_delay_seconds: float = 0.5,
        user_agent: str = "GovAIAdministrativeAcquisition/1.0 (+local research)",
    ):
        self.allowed_domains = {domain.casefold() for domain in allowed_domains}
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.retry_count = retry_count
        self.rate_delay_seconds = rate_delay_seconds
        self.user_agent = user_agent
        self._last_request = 0.0

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise NetworkSafetyError("Only HTTPS external acquisition URLs are allowed")
        if parsed.username or parsed.password:
            raise NetworkSafetyError("Credentials in external URLs are forbidden")
        if (parsed.hostname or "").casefold() not in self.allowed_domains:
            raise NetworkSafetyError("URL domain is not in the configured source adapter allowlist")

    def fetch(self, url: str, *, expected_pdf: bool = False) -> FetchResult:
        self._validate_url(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.bin"
        meta_path = self.cache_dir / f"{cache_key}.meta"
        if body_path.is_file() and meta_path.is_file():
            final_url, content_type = meta_path.read_text(encoding="utf-8").split("\n", 1)
            body = body_path.read_bytes()
            self._validate_payload(body, content_type, expected_pdf)
            return FetchResult(url, final_url, content_type, body)

        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                remaining = self.rate_delay_seconds - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
                redirect = _BoundedRedirectHandler(self.allowed_domains, self.max_redirects)
                opener = urllib.request.build_opener(redirect)
                request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                self._last_request = time.monotonic()
                with opener.open(request, timeout=self.timeout_seconds) as response:
                    final_url = response.geturl()
                    self._validate_url(final_url)
                    content_type = response.headers.get_content_type().casefold()
                    length = response.headers.get("Content-Length")
                    if length and int(length) > self.max_bytes:
                        raise NetworkSafetyError("Response exceeds the configured maximum size")
                    body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise NetworkSafetyError("Response exceeds the configured maximum size")
                self._validate_payload(body, content_type, expected_pdf)
                body_path.write_bytes(body)
                meta_path.write_text(f"{final_url}\n{content_type}", encoding="utf-8")
                return FetchResult(url, final_url, content_type, body)
            except (OSError, urllib.error.URLError, NetworkSafetyError) as error:
                last_error = error
                if attempt >= self.retry_count:
                    break
        raise NetworkSafetyError(str(last_error or "External request failed"))

    @staticmethod
    def _validate_payload(body: bytes, content_type: str, expected_pdf: bool) -> None:
        if expected_pdf and (content_type != "application/pdf" or not body.startswith(b"%PDF")):
            raise NetworkSafetyError("Expected a PDF but received HTML, an error payload, or an invalid signature")


__all__ = ["FetchResult", "NetworkSafetyError", "SafeHttpClient"]
