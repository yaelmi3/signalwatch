"""Discover articles from configured RSS feeds."""

import sqlite3
from xml.etree import ElementTree

import httpx2
import structlog

from signalwatch import storage
from signalwatch.article_content import TransientFetchError, public_http_url, read_public_content

_MAX_ARTICLE_URL_CHARS = 2083
feed_logger = structlog.get_logger()


class _RssTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("RSS documents cannot contain a doctype.")


def parse_feed(content: bytes, feed_url: str) -> list[storage.Article]:
    root = ElementTree.fromstring(content, parser=ElementTree.XMLParser(target=_RssTreeBuilder()))
    channel = root.find("channel")
    if root.tag != "rss" or root.get("version") != "2.0" or channel is None:
        raise ValueError("Expected an RSS 2.0 channel.")

    listing_links = {
        _link_identity(url)
        for value in (feed_url, channel.findtext("link", "").strip())
        if (url := public_http_url(value)) is not None
    }
    articles = []
    for item in channel.findall("item"):
        title_element = item.find("title")
        title = "".join(title_element.itertext()).strip() if title_element is not None else ""
        link = item.findtext("link", "").strip()
        if not title or len(link) > _MAX_ARTICLE_URL_CHARS:
            continue
        url = public_http_url(link)
        if url is None or _link_identity(url) in listing_links:
            continue
        comments = public_http_url(item.findtext("comments", "").strip())
        if comments is None or _link_identity(url) != _link_identity(comments):
            articles.append(storage.Article(title=title, article_url=link))
    return articles


def _link_identity(url: httpx2.URL) -> tuple[bytes, bytes, bytes]:
    path = url.raw_path.partition(b"?")[0].rstrip(b"/")
    return url.raw_host.rstrip(b"."), path, url.query


def sync_feeds() -> None:
    try:
        feeds = storage.list_article_feeds()
    except (sqlite3.OperationalError, OSError) as error:
        feed_logger.warning("Could not list article feeds", error_type=type(error).__name__)
        return
    for feed in feeds:
        try:
            content = read_public_content(feed.url)
            if content is not None:
                storage.save_discovered_articles(feed, parse_feed(content.body, feed.url))
        except (
            TransientFetchError,
            ElementTree.ParseError,
            LookupError,
            ValueError,
            sqlite3.OperationalError,
            OSError,
        ) as error:
            feed_logger.warning(
                "Could not sync article feed", source_id=feed.source_id, error_type=type(error).__name__
            )
