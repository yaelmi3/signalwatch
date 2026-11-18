"""Fetch bounded public content and extract publisher evidence."""

import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import IPv4Address, IPv6Address, ip_address
from ssl import SSLCertVerificationError
from time import monotonic

import httpx2
import structlog

_DEFAULT_PORT = {"http": 80, "https": 443}
_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 512 * 1024
_MAX_EVIDENCE_CHARS = 12_000
_SKIP_TAGS = {"head", "header", "nav", "aside", "footer", "script", "style", "template", "noscript"}
_PUBLISHER_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
article_logger = structlog.get_logger()


class TransientFetchError(Exception):
    """A temporary fetch failure with a message safe for workflow logs."""


@dataclass(frozen=True)
class PublicContent:
    body: bytes
    content_type: str
    encoding: str


def read_publisher_text(article_url: str) -> str | None:
    content = read_public_content(article_url, _PUBLISHER_TYPES)
    if content is None:
        return None
    try:
        text = content.body.decode(content.encoding, errors="replace")
        if content.content_type == "text/plain":
            return text.strip()[:_MAX_EVIDENCE_CHARS] or None
        parser = _PublisherParagraphs()
        parser.feed(text)
    except (LookupError, ValueError) as error:
        article_logger.warning("Could not extract publisher text", error_type=type(error).__name__)
        return None
    paragraphs = (" ".join("".join(parts).split()) for parts in parser.paragraphs)
    return "\n\n".join(filter(None, paragraphs))[:_MAX_EVIDENCE_CHARS] or None


def public_http_url(value: str) -> httpx2.URL | None:
    if not value.lower().startswith(("https://", "http://")) or "\\" in value:
        return None
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        url = httpx2.URL(value)
    except (httpx2.InvalidURL, ValueError):
        return None
    if url.port not in (None, _DEFAULT_PORT[url.scheme]) or not url.raw_host or url.userinfo:
        return None
    host = url.raw_host.decode("ascii").rstrip(".")
    try:
        address = ip_address(host)
    except ValueError:
        if "." not in host or "" in host.split(".") or host.endswith((".localhost", ".local")):
            return None
        try:
            socket.inet_aton(host)
        except OSError:
            return url
        return None
    return url if _is_public_address(address) else None


def read_public_content(url: str, content_types: frozenset[str] | None = None) -> PublicContent | None:
    parsed = public_http_url(url)
    if parsed is None:
        return None
    try:
        address = _public_address(parsed.raw_host.decode("ascii"), _DEFAULT_PORT[parsed.scheme])
        if address is not None:
            return _read_content(url=parsed, address=address, content_types=content_types)
    except (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RemoteProtocolError, TimeoutError) as error:
        # The sync HTTP transport wraps the SSL error twice.
        cause = error.__cause__
        if cause is None or not isinstance(cause.__cause__, SSLCertVerificationError):
            raise TransientFetchError(type(error).__name__) from None
        article_logger.warning("Public content TLS certificate rejected")
    except httpx2.HTTPStatusError as error:
        status = error.response.status_code
        if status in {408, 429} or 500 <= status < 600:
            raise TransientFetchError(f"HTTP {status}") from None
        article_logger.warning("Public content request failed", status=status)
    except socket.gaierror as error:
        if error.errno == socket.EAI_AGAIN:
            raise TransientFetchError("Temporary DNS failure") from None
        article_logger.warning("Public content DNS lookup failed", error_type=type(error).__name__)
    except (httpx2.HTTPError, httpx2.InvalidURL, OSError, ValueError) as error:
        article_logger.warning("Could not read public content", error_type=type(error).__name__)
    return None


def _is_public_address(address: IPv4Address | IPv6Address) -> bool:
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    return not isinstance(address, IPv6Address) or (not address.is_site_local and address.scope_id is None)


def _public_address(hostname: str, port: int) -> str | None:
    try:
        return str(ip_address(hostname))
    except ValueError:
        endpoints = socket.getaddrinfo(
            f"{hostname.rstrip('.')}.", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    addresses = (ip_address(endpoint[0]) for *_, endpoint in endpoints)
    return next((str(address) for address in addresses if _is_public_address(address)), None)


def _read_content(*, url: httpx2.URL, address: str, content_types: frozenset[str] | None) -> PublicContent | None:
    headers = {"Host": url.netloc.decode("ascii"), "Accept-Encoding": "identity"}
    deadline = monotonic() + _TIMEOUT_SECONDS
    with httpx2.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        # Connect to the validated address while keeping the original Host and TLS identity.
        with client.stream(
            "GET",
            url.copy_with(host=address, fragment=None),
            headers=headers,
            extensions={"sni_hostname": url.raw_host.decode("ascii")},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if content_types is not None and content_type not in content_types:
                return None
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return None
            content = bytearray()
            for chunk in response.iter_raw():
                if monotonic() >= deadline:
                    raise TimeoutError("Public content body read timed out")
                content.extend(chunk[: _MAX_BYTES - len(content)])
                if len(content) == _MAX_BYTES:
                    break
            return PublicContent(bytes(content), content_type, response.encoding or "utf-8")


class _PublisherParagraphs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paragraphs: list[list[str]] = []
        self._skipping: list[str] = []
        self._in_paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skipping.append(tag)
        elif not self._skipping and tag == "p":
            self._in_paragraph = True
            self.paragraphs.append([])
        elif self._in_paragraph and not self._skipping and tag == "br":
            self.paragraphs[-1].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skipping:
            if tag == self._skipping[-1]:
                self._skipping.pop()
        elif tag == "p":
            self._in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self._in_paragraph and not self._skipping:
            self.paragraphs[-1].append(data)
