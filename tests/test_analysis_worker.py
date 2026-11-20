import json
import socket
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from typing import Any

import httpx2
import pytest
from dbos import DBOS
from openai import OpenAI

from signalwatch import analysis_worker, article_content, article_feeds, slack_delivery, storage
from signalwatch.analysis_models import ChangeAnalysisResult, TeamDecision, TeamInterests


def test_worker_retries_handoff_and_limits_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    app_db = tmp_path / "test configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(app_db))
    monkeypatch.setenv("SIGNALWATCH_ANALYSIS_MODEL", "test-model")
    monkeypatch.setenv("SIGNALWATCH_ANALYSIS_HIGHER_QUALITY_MODEL", "test-alternate-model")
    monkeypatch.setenv("SIGNALWATCH_SLACK_BOT_TOKEN", "fake-slack-token")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CHANNEL_ID", "C_TEST")
    storage.save_team_interests(
        TeamInterests(
            marketing="Customer stories", product="Product releases", business_context="We build marine instruments."
        )
    )
    baseline = storage.Article(title="Baseline", article_url="https://1.1.1.1/baseline")
    article = storage.Article(title="New sensor", article_url="https://1.1.1.1/sensor")
    unreadable = storage.Article(title="Blocked article", article_url="https://1.1.1.1/blocked")
    for name in ("Engineering", "Technology"):
        source = storage.save_competitor(name, f"https://{name.lower()}.test/")
        storage.set_article_feed(source_id=source.id, url=f"https://{name.lower()}.test/feed")
    feeds = storage.list_article_feeds()
    for feed in feeds:
        storage.save_discovered_articles(feed, [baseline])
    page = storage.save_competitor("Example <!here>", "https://example.test/").pages[0]
    storage.link_watch(page.id, "test-watch")
    for version, before in enumerate((None, "Release 1", "Release 2", "Release 3"), start=1):
        assert storage.save_page_version(
            page_id=page.id, watch_id="test-watch", version=version, before=before, after=f"Release {version}"
        )
    assert storage.save_page_version(page_id=page.id, watch_id="test-watch", version=0, before="Old", after="Archived")
    held = TeamDecision(relevant=False, reason="No match.", quote=None)
    old_result = ChangeAnalysisResult(
        summary="Archived",
        marketing=held,
        product=TeamDecision(relevant=True, reason="An old release.", quote="Archived"),
        research_and_development=held,
    )
    storage.save_change_analysis(page.id, 0, old_result)
    storage.save_change_analysis(page.id, 0, old_result, slack_channels=slack_delivery.configured_channels())

    original_mark = storage.mark_analysis_queued
    handoffs: Counter[int] = Counter()

    def interrupt_first_handoff(page_id: int, version: int) -> None:
        handoffs[version] += 1
        if version == 2 and handoffs[version] == 1:
            raise sqlite3.OperationalError("interrupted after enqueue")
        original_mark(page_id, version)

    monkeypatch.setattr(storage, "mark_analysis_queued", interrupt_first_handoff)
    monkeypatch.setattr(article_feeds, "sync_feeds", lambda: None)
    release = Event()
    first_two_running = Event()
    stop = Event()
    lock = Lock()
    attempts: Counter[int | str] = Counter()
    publisher_reads: Counter[str] = Counter()
    change = "The new tide sensor logs wave height every minute."
    other_change = "A customer described a previous sensor prototype."
    publisher_text = f"{change.removesuffix('.')}\n{change}\n{other_change}\nThe vendor redesigned the help page."
    active = peak = 0
    slack_posts: list[str] = []

    def publisher_respond(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == "https://slack.com/api/chat.postMessage":
            assert request.method == HTTPMethod.POST and request.headers["authorization"] == "Bearer fake-slack-token"
            payload = json.loads(request.content)
            assert payload == {
                "channel": "C_TEST",
                "text": payload["text"],
                "mrkdwn": False,
                "link_names": False,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            slack_posts.append(payload["text"])
            return httpx2.Response(HTTPStatus.OK, json={"ok": True})
        with lock:
            publisher_reads[url] += 1
            attempt = publisher_reads[url]
        if url == unreadable.article_url:
            return httpx2.Response(HTTPStatus.NOT_FOUND)
        assert url == article.article_url
        if attempt == 1:
            return httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE, text="private publisher error text")
        return httpx2.Response(
            HTTPStatus.OK, headers={"Content-Type": "text/plain"}, stream=httpx2.ByteStream(publisher_text.encode())
        )

    class TestClient(httpx2.Client):
        def __init__(self, *, transport: httpx2.BaseTransport | None = None, **kwargs: Any) -> None:
            super().__init__(transport=transport or httpx2.MockTransport(publisher_respond), **kwargs)

    monkeypatch.setattr(httpx2, "Client", TestClient)

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal active, peak
        body = json.loads(request.content)
        assert body["store"] is False
        evidence = json.loads(body["input"])
        assert evidence["business_context"] == "We build marine instruments."
        assert evidence["team_interests"] == {
            "marketing": "Customer stories",
            "product": "Product releases",
            "research_and_development": "",
        }
        if "publisher_text" in evidence:
            assert evidence["headline"] == article.title
            assert evidence["publisher_text"] == publisher_text
            item, summary = "article-draft", publisher_text
        elif "candidate_passages" in evidence:
            assert evidence["candidate_passages"] == [change, other_change]
            assert set(evidence) == {"candidate_passages", "team_interests", "business_context"}
            item, summary = "article-verifier", "The verifier's summary should not be saved."
        else:
            summary = evidence["after"][0]
            item = int(summary.removeprefix("Release "))
        required_fields = body["text"]["format"]["schema"]["required"]
        assert ("change_quote" in required_fields) == (item == "article-verifier")
        with lock:
            attempts[item] += 1
            attempt = attempts[item]
            active += 1
            peak = max(peak, active)
            if all(attempts[v] for v in (2, 3)):
                first_two_running.set()
        try:
            expected_model = "test-model" if item in {2, 3} and attempt == 1 else "test-alternate-model"
            assert body["model"] == expected_model
            if item in {2, 3} and attempt == 1:
                assert release.wait(12)
            if item == 4 or (item == 2 and attempt == 1):
                return httpx2.Response(503, json={"error": {"message": "private provider error text"}})
            quote = summary if item == 2 else "invented quote"
            if item == "article-verifier":
                return _model_response(
                    summary=summary,
                    quote="logs wave height every minute.",
                    marketing_quote=other_change,
                    change_quote=change if attempt == 1 else f"{change}\n{other_change}",
                )
            return _model_response(
                summary=summary,
                quote="logs wave height every minute" if item == "article-draft" else quote,
                marketing_quote=other_change if item == "article-draft" else None,
            )
        finally:
            with lock:
                active -= 1

    def model_client(max_retries: int, timeout: int) -> OpenAI:
        return OpenAI(
            api_key="fake-test-key",
            max_retries=max_retries,
            timeout=timeout,
            http_client=TestClient(transport=httpx2.MockTransport(respond)),
        )

    monkeypatch.setattr(analysis_worker, "OpenAI", model_client)

    def held_article_statuses() -> dict[tuple[int, str], str]:
        return {
            (row.source_id, row.article_url): row.status
            for row in storage.list_digest(storage.DigestView.HELD)
            if isinstance(row, storage.DigestArticle)
        }

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(analysis_worker.run_worker, stop)
        try:
            assert first_two_running.wait(10)
            storage.save_discovered_articles(feeds[0], [article, unreadable])
            storage.save_discovered_articles(feeds[1], [article])
            blocked_key = (feeds[0].source_id, unreadable.article_url)
            queued_articles = {(feed.source_id, article.article_url): "queued" for feed in feeds}
            queued_articles[blocked_key] = "queued"
            _wait_until(lambda: held_article_statuses() == queued_articles)
            assert not publisher_reads
            storage.save_analysis_quality(analysis_worker.AnalysisQuality.HIGHER_QUALITY)
            release.set()
            _wait_until(lambda: [workflow.status for workflow in DBOS.list_workflows()] == ["SUCCESS"] * 6)
            _wait_until(lambda: len(slack_posts) == 3)
        finally:
            release.set()
            stop.set()
            worker.result(timeout=15)

    analyses = storage.list_page_analyses(page.id)
    assert peak == 2
    assert handoffs == {2: 2, 3: 1, 4: 1}
    assert attempts == {2: 2, 3: 1, 4: 3, "article-draft": 2, "article-verifier": 2}
    assert publisher_reads == {article.article_url: 3, unreadable.article_url: 1}
    relevant_articles = [row for row in storage.list_digest() if isinstance(row, storage.DigestArticle)]
    assert [(row.article_url, row.status, row.summary, row.relevant_teams) for row in relevant_articles] == [
        (article.article_url, "completed", publisher_text, ["product"]),
    ]
    held_source = next(feed.source_id for feed in feeds if feed.source_id != relevant_articles[0].source_id)
    assert held_article_statuses() == {blocked_key: "failed", (held_source, article.article_url): "completed"}
    assert analyses["2"].status == "completed"
    assert analyses["2"].result is not None
    assert analyses["2"].result.product.relevant
    assert analyses["2"].result.product.quote == "Release 2"
    assert analyses["2"].result.research_and_development.model_dump() == {
        "relevant": False,
        "reason": "No interests are configured for this team.",
        "quote": None,
    }
    assert analyses["3"].status == "completed"
    assert analyses["3"].result is not None
    assert analyses["3"].result.product.quote is None
    assert analyses["4"].model_dump() == {"status": "failed", "result": None}
    assert Counter(post.splitlines()[0] for post in slack_posts) == Counter(
        [
            "Example &lt;!here&gt;",
            "Example &lt;!here&gt;",
            f"{relevant_articles[0].source_name} · New sensor",
        ]
    )
    assert {post.splitlines()[-1] for post in slack_posts} == {page.url, article.article_url}
    assert all("Product: An updated release is described." in post and "Archived" not in post for post in slack_posts)
    captured = capfd.readouterr()
    assert "private provider error text" not in captured.out + captured.err
    assert "private publisher error text" not in captured.out + captured.err


def test_slack_rejections_and_ambiguous_outcomes_are_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    app_db = tmp_path / "app.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(app_db))
    page = storage.save_competitor("Example", "https://example.test/").pages[0]
    storage.link_watch(page.id, "test-watch")
    channels = {storage.DigestTeam.PRODUCT: "C_TEST"}
    held = TeamDecision(relevant=False, reason="No match.", quote=None)
    result = ChangeAnalysisResult(
        summary="New release",
        marketing=held,
        product=TeamDecision(relevant=True, reason="New release.", quote="New"),
        research_and_development=held,
    )
    for version in (1, 2):
        assert storage.save_page_version(
            page_id=page.id, watch_id="test-watch", version=version, before="Old", after="New"
        )
        storage.save_change_analysis(page.id, version, result, slack_channels=channels)

    attempts = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        if len(attempts) == 1:
            body = {"ok": False, "error": "not_in_channel", "detail": "private-text"}
            return httpx2.Response(HTTPStatus.OK, json=body)
        raise httpx2.ReadTimeout("private-text", request=request)

    class TestClient(httpx2.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx2, "Client", TestClient)
    settings = slack_delivery.SlackSettings(channels, "fake-slack-token")
    for _ in range(3):
        slack_delivery.send_next(settings)
    assert len(attempts) == 2
    with sqlite3.connect(app_db) as db:
        assert db.execute("SELECT status, error FROM slack_deliveries ORDER BY id").fetchall() == [
            ("failed", "not_in_channel"),
            ("uncertain", "transport_error"),
        ]
    logs = capfd.readouterr()
    assert "private-text" not in logs.out + logs.err and "fake-slack-token" not in logs.out + logs.err


def test_slack_migrates_saved_channel_and_groups_team_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_db = tmp_path / "app.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(app_db))
    page = storage.save_competitor("Example", "https://example.test/").pages[0]
    storage.link_watch(page.id, "test-watch")
    result = ChangeAnalysisResult(
        summary="New release",
        marketing=TeamDecision(relevant=True, reason="New positioning.", quote="New"),
        product=TeamDecision(relevant=True, reason="New feature.", quote="New"),
        research_and_development=TeamDecision(relevant=True, reason="New API.", quote="New"),
    )
    for version in (1, 2, 3):
        storage.save_page_version(page_id=page.id, watch_id="test-watch", version=version, before="Old", after="New")
    storage.save_change_analysis(page.id, 1, result)
    storage.save_change_analysis(page.id, 2, result)
    with sqlite3.connect(app_db) as db:
        db.executescript("""
            DROP TABLE slack_connection;
            CREATE TABLE slack_connection (id INTEGER PRIMARY KEY, workspace_name TEXT, encrypted_token TEXT,
                                           channel_id TEXT, enabled INTEGER);
            INSERT INTO slack_connection VALUES (1, 'Test', 'encrypted', 'C_OLD', 1);
            DROP TABLE slack_deliveries;
            CREATE TABLE slack_deliveries (id INTEGER PRIMARY KEY, channel_id TEXT, page_id INTEGER, version INTEGER,
                                           source_id INTEGER, article_url TEXT, status TEXT, error TEXT,
                                           UNIQUE (page_id, version), UNIQUE (source_id, article_url));
            """)
        db.executemany(
            "INSERT INTO slack_deliveries VALUES (?, 'C_OLD', ?, ?, NULL, NULL, ?, NULL)",
            [(1, page.id, 1, "sent"), (2, page.id, 2, "pending")],
        )

    connection = storage.get_slack_connection()
    assert connection and connection.enabled and connection.team_channels == dict.fromkeys(storage.DigestTeam, "C_OLD")
    posts: list[tuple[str, str]] = []
    monkeypatch.setattr(slack_delivery, "post_message", lambda channel, token, text: posts.append((channel, text)))
    slack_delivery.send_next(slack_delivery.SlackSettings(connection.team_channels, "fake", connection))
    assert [channel for channel, _ in posts] == ["C_OLD"]

    channels = dict(zip(storage.DigestTeam, ("C_SHARED", "C_SHARED", "C_RD"), strict=True))
    connection = storage.save_slack_destination(connection, channels, True)
    assert connection is not None
    storage.save_change_analysis(page.id, 3, result, slack_channels=channels)
    storage.save_change_analysis(page.id, 3, result, slack_channels=channels)
    for _ in range(3):
        slack_delivery.send_next(slack_delivery.SlackSettings(channels, "fake", connection))

    assert [channel for channel, _ in posts] == ["C_OLD", "C_SHARED", "C_RD"]
    assert posts[1][1].splitlines()[3:-2] == ["Marketing: New positioning.", "Product: New feature."]
    assert posts[2][1].splitlines()[3:-2] == ["R&D: New API."]
    with sqlite3.connect(app_db) as db:
        assert db.execute("SELECT id, status FROM slack_deliveries ORDER BY id").fetchall() == [
            (1, "sent"), (2, "sent"), (3, "sent"), (4, "sent"),
        ]


@pytest.mark.parametrize(
    ("separator", "before_edits", "after_edits"),
    [
        (
            "\n",
            ["Old opening", "Old price $10 " + "old text " * 600, "Old closing"],
            ["New opening", "New price $20 " + "new detail " * 600, "New closing"],
        ),
        (" ", ["Old opening", "Old price $10", "Old closing"], ["Different opening", "Sale price $20", "New closing"]),
    ],
    ids=["uneven-sizes", "single-line"],
)
def test_evidence_keeps_scattered_edits_on_a_long_page(
    separator: str, before_edits: list[str], after_edits: list[str]
) -> None:
    padding = f"Unchanged text{separator}" * 2000
    change = storage.ChangeToAnalyze(
        before=padding + padding.join(f"{edit}{separator}" for edit in before_edits) + padding,
        after=padding + padding.join(f"{edit}{separator}" for edit in after_edits) + padding,
    )

    evidence = analysis_worker._change_evidence(change)

    for side, edits in (("before", before_edits), ("after", after_edits)):
        assert all(any(edit in passage for passage in evidence[side]) for edit in edits)
        assert sum(map(len, evidence[side])) <= 12_000


def test_feed_sync_pins_requests_and_isolates_failed_feeds(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = storage.ArticleFeed(3, "https://source.test/feed.xml")
    feeds = [
        storage.ArticleFeed(1, "http://127.1/feed.xml"),
        storage.ArticleFeed(2, "https://broken.test/feed.xml"),
        feed,
    ]
    saved: list[tuple[storage.ArticleFeed, list[storage.Article]]] = []
    requested = []
    body = b"""<rss version="2.0"><channel><link>https://source.test/</link>
      <item><title>Listing</title><link>http://source.test/#latest</link></item>
      <item><title>Feed</title><link>https://source.test/feed.xml</link></item>
      <item><title>Discussion</title><link>https://source.test/discussion</link>
        <comments>https://source.test/discussion#comments</comments></item>
      <item><title>Private</title><link>http://0x7f.0.0.1/</link></item>
      <item><title>Credentials</title><link>https://user:password@publisher.test/</link></item>
      <item><title>Release &amp; details</title><link>https://source.test/story?edition=1&amp;ref=rss</link></item>
    </channel></rss>"""

    def resolve(host: str, port: int, **_kwargs: int) -> list[tuple]:
        assert host in {"broken.test.", "source.test."}
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", port))]

    def respond(request: httpx2.Request) -> httpx2.Response:
        host = request.headers["host"]
        requested.append(host)
        assert str(request.url) == "https://1.1.1.1/feed.xml"
        assert request.extensions["sni_hostname"] == host
        assert "authorization" not in request.headers and "cookie" not in request.headers
        content = b'<!DOCTYPE rss [<!ENTITY name "Example">]>' + body if host == "broken.test" else body
        return httpx2.Response(HTTPStatus.OK, stream=httpx2.ByteStream(content))

    def save_articles(source_feed: storage.ArticleFeed, articles: list[storage.Article]) -> None:
        saved.append((source_feed, articles))

    monkeypatch.setattr(storage, "list_article_feeds", lambda: feeds)
    monkeypatch.setattr(storage, "save_discovered_articles", save_articles)
    monkeypatch.setattr(article_content.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(respond)))

    article_feeds.sync_feeds()

    assert requested == ["broken.test", "source.test"]
    assert saved == [(feed, [storage.Article("Release & details", "https://source.test/story?edition=1&ref=rss")])]


@pytest.mark.parametrize("status", [HTTPStatus.OK, HTTPStatus.FOUND])
def test_publisher_fetch_pins_public_address_and_preserves_host(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def resolve(host: str, port: int, **kwargs: int) -> list[tuple]:
        assert host == "publisher.test."
        assert port == 443
        assert kwargs == {"type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP}
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in ("10.0.0.7", "1.1.1.1")
        ]

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://1.1.1.1/story?edition=2"
        assert request.headers["host"] == request.extensions["sni_hostname"] == "publisher.test"
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        html = b"<nav><p>Menu</p></nav><article><h2>Launch</h2><p><em>Sensor</em> shipped &amp; tested.</p></article>"
        return httpx2.Response(
            status, headers={"Content-Type": "text/html", "Location": "/another"}, stream=httpx2.ByteStream(html)
        )

    monkeypatch.setattr(article_content.socket, "getaddrinfo", resolve)
    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(article_content.httpx2, "Client", partial(httpx2.Client, transport=transport))

    evidence = article_content.read_publisher_text("https://publisher.test/story?edition=2#section")

    assert evidence == ("Launch\nSensor shipped & tested." if status == HTTPStatus.OK else None)


@pytest.mark.parametrize(
    "url",
    [
        "https://publisher.test:8443/story",
        "https://user@publisher.test/story",
        "http://127.0.0.1/",
        "http://127.1/",
        "https://[fec0::1]/",
        "https://private.test/story",
    ],
)
def test_publisher_fetch_never_connects_to_disallowed_destinations(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    def resolve(host: str, port: int, **_kwargs: int) -> list[tuple]:
        assert host == "private.test."
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", port))]

    def reject_connection(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("An unsafe publisher destination was requested")

    monkeypatch.setattr(article_content.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        article_content.httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(reject_connection))
    )

    assert article_content.read_publisher_text(url) is None


def test_publisher_fetch_stops_at_response_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class PublisherStream(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"New tides."
            yield b" High waves."
            pytest.fail("The publisher response was read past the byte cap")

    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(HTTPStatus.OK, headers={"Content-Type": "text/plain"}, stream=PublisherStream())

    monkeypatch.setattr(article_content, "_MAX_BYTES", 15)
    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(article_content.httpx2, "Client", partial(httpx2.Client, transport=transport))

    assert article_content.read_publisher_text("https://1.1.1.1/story") == "New tides. High"


def _model_response(
    *, summary: str, quote: str, marketing_quote: str | None = None, change_quote: str | None = None
) -> httpx2.Response:
    marketing = {"relevant": marketing_quote is not None, "reason": "A customer is quoted.", "quote": marketing_quote}
    result = {
        "summary": summary,
        "marketing": marketing,
        "research_and_development": {"relevant": True, "reason": "A new release was listed.", "quote": summary},
        "product": {"relevant": True, "reason": "An updated release is described.", "quote": quote},
    }
    if change_quote is not None:
        result["change_quote"] = change_quote
    return httpx2.Response(
        HTTPStatus.OK,
        json={
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(result), "annotations": []}],
                }
            ],
        },
    )


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.05)
    assert predicate()
