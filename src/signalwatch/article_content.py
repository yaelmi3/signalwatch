"""Read bounded evidence from public publisher URLs."""

import socket
from html.parser import HTMLParser
from ipaddress import IPv6Address, ip_address
from time import monotonic

import httpx2
import structlog

_DEFAULT_PORT = {"http": 80, "https": 443}
_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 512 * 1024
_MAX_EVIDENCE_CHARS = 12_000
_SKIP_TAGS = {"head", "header", "nav", "aside", "footer", "script", "style", "template", "noscript"}
article_logger = structlog.get_logger()


def read_publisher_text(article_url: str) -> str | None:
    try:
        url = httpx2.URL(article_url)
        port = _DEFAULT_PORT.get(url.scheme)
        if port is None or url.port not in (None, port) or not url.raw_host or url.userinfo:
            return None
        hostname = url.raw_host.decode("ascii")
        address = _public_address(hostname, port)
        if address is None:
            return None

        evidence = _read_text(url, address, hostname)
        return evidence or None
    except (httpx2.HTTPError, httpx2.InvalidURL, OSError, ValueError, LookupError) as error:
        article_logger.warning("Could not read publisher text", error_type=type(error).__name__)
        return None


def _public_address(hostname: str, port: int) -> str | None:
    try:
        addresses = [ip_address(hostname)]
    except ValueError:
        dns_name = hostname.rstrip(".")
        if "." not in dns_name or dns_name.endswith((".localhost", ".local")):
            return None
        addresses = [
            ip_address(endpoint[0])
            for *_, endpoint in socket.getaddrinfo(
                f"{dns_name}.", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        ]
    for address in addresses:
        if not address.is_global or address.is_multicast or address.is_reserved:
            continue
        if isinstance(address, IPv6Address) and (address.is_site_local or address.scope_id is not None):
            continue
        return str(address)
    return None


def _read_text(url: httpx2.URL, address: str, hostname: str) -> str:
    headers = {"Host": url.netloc.decode("ascii"), "Accept-Encoding": "identity"}
    deadline = monotonic() + _TIMEOUT_SECONDS
    with httpx2.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        # Use the validated numeric destination while keeping the publisher's Host and TLS identity.
        with client.stream(
            "GET", url.copy_with(host=address, fragment=None), headers=headers, extensions={"sni_hostname": hostname}
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                return ""
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return ""
            content = bytearray()
            for chunk in response.iter_raw():
                if monotonic() >= deadline:
                    raise TimeoutError("Publisher body read timed out")
                content.extend(chunk[: _MAX_BYTES - len(content)])
                if len(content) == _MAX_BYTES:
                    break
            text = content.decode(response.encoding or "utf-8", errors="replace")
    if content_type == "text/plain":
        return text.strip()[:_MAX_EVIDENCE_CHARS]
    parser = _PublisherParagraphs()
    parser.feed(text)
    paragraphs = (" ".join("".join(parts).split()) for parts in parser.paragraphs)
    return "\n\n".join(filter(None, paragraphs))[:_MAX_EVIDENCE_CHARS]


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
