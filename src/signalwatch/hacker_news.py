import sqlite3
from http import HTTPMethod
from ipaddress import ip_address
from xml.etree import ElementTree

import httpx2
import structlog
from pydantic import HttpUrl, TypeAdapter, ValidationError

from signalwatch import storage
from signalwatch.network import send_request

_FEED_URL = "https://news.ycombinator.com/rss"
_ARTICLE_URL = TypeAdapter(HttpUrl)
hacker_news_logger = structlog.get_logger()


def parse_feed(content: bytes) -> list[storage.HackerNewsArticle]:
    root = ElementTree.fromstring(content)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected an RSS channel.")

    articles = []
    for item in root.findall("channel/item"):
        title_element = item.find("title")
        title = "".join(title_element.itertext()).strip() if title_element is not None else ""
        link = item.findtext("link", "").strip()
        if title and _is_publisher_link(link):
            articles.append(storage.HackerNewsArticle(title=title, article_url=link))
    return articles


def sync_feed() -> None:
    try:
        if storage.has_hacker_news_source():
            response = send_request(method=HTTPMethod.GET, url=_FEED_URL, headers={})
            storage.save_hacker_news_articles(parse_feed(response.content))
    except (httpx2.HTTPError, ElementTree.ParseError, ValueError, sqlite3.OperationalError, OSError) as error:
        hacker_news_logger.warning("Could not sync Hacker News articles.", error_type=type(error).__name__)


def _is_publisher_link(link: str) -> bool:
    if not link.lower().startswith(("https://", "http://")) or "\\" in link:
        return False
    if any(character.isspace() or ord(character) < 32 for character in link):
        return False
    try:
        url = _ARTICLE_URL.validate_python(link)
    except ValidationError:
        return False
    if url.host is None or url.username is not None or url.password is not None:
        return False
    host = url.host.rstrip(".").lower()
    if host == "news.ycombinator.com":
        return False
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        return "." in host and not host.endswith((".localhost", ".local"))
    return address.is_global and not address.is_multicast
