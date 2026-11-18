import json
import socket
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep

import httpx2
import pytest
from dbos import DBOS
from openai import OpenAI

from signalwatch import analysis_worker, article_content, storage
from signalwatch.analysis_models import TeamInterests


def test_worker_retries_handoff_and_limits_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    app_db = tmp_path / "test configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(app_db))
    monkeypatch.setenv("SIGNALWATCH_ANALYSIS_MODEL", "test-model")
    monkeypatch.setenv("SIGNALWATCH_ANALYSIS_HIGHER_QUALITY_MODEL", "test-alternate-model")
    storage.save_team_interests(
        TeamInterests(
            marketing="Customer stories", product="Product releases", business_context="We build marine instruments."
        )
    )
    storage.save_competitor("Hacker News", "https://news.ycombinator.com/")
    baseline = storage.HackerNewsArticle(title="Baseline", article_url="https://publisher.test/baseline")
    article = storage.HackerNewsArticle(title="New sensor", article_url="https://publisher.test/sensor")
    unreadable = storage.HackerNewsArticle(title="Blocked article", article_url="https://publisher.test/blocked")
    storage.save_hacker_news_articles([baseline])
    page = storage.save_competitor("Example", "https://example.test/").pages[0]
    storage.link_watch(page.id, "test-watch")
    for version, before in enumerate((None, "Release 1", "Release 2", "Release 3"), start=1):
        assert storage.save_page_version(
            page_id=page.id, watch_id="test-watch", version=version, before=before, after=f"Release {version}"
        )

    original_mark = storage.mark_analysis_queued
    handoffs: Counter[int] = Counter()

    def interrupt_first_handoff(page_id: int, version: int) -> None:
        handoffs[version] += 1
        if version == 2 and handoffs[version] == 1:
            raise sqlite3.OperationalError("interrupted after enqueue")
        original_mark(page_id, version)

    monkeypatch.setattr(storage, "mark_analysis_queued", interrupt_first_handoff)
    monkeypatch.setattr(analysis_worker.hacker_news, "sync_feed", lambda: None)
    release = Event()
    first_two_running = Event()
    third_started = Event()
    stop = Event()
    lock = Lock()
    attempts: Counter[int | str] = Counter()
    publisher_reads: Counter[str] = Counter()
    publisher_text = "The new tide sensor logs wave height every minute."
    active = peak = 0

    def read_publisher_text(url: str) -> str | None:
        with lock:
            publisher_reads[url] += 1
        return publisher_text if url == article.article_url else None

    monkeypatch.setattr(article_content, "read_publisher_text", read_publisher_text)

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal active, peak
        body = json.loads(request.content)
        assert body["store"] is False
        schema = body["text"]["format"]["schema"]
        assert set(schema["required"]) == {"summary", "marketing", "product", "research_and_development"}
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
            assert "Hacker News discovery" in body["instructions"]
            item, summary = "article", publisher_text
        else:
            summary = evidence["after"][0]
            item = int(summary.removeprefix("Release "))
            assert evidence["before"] == [f"Release {item - 1}"]
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
            if item in {4, "article"}:
                third_started.set()
            elif attempt == 1:
                assert release.wait(12)
            if item == 4 or (item in {2, "article"} and attempt == 1):
                return httpx2.Response(503, json={"error": {"message": "private provider error text"}})
            return _model_response(
                summary=summary,
                quote=summary if item in {2, "article"} else "invented quote",
                marketing_quote="invented marketing quote" if item == "article" else None,
            )
        finally:
            with lock:
                active -= 1

    def model_client(max_retries: int, timeout: int) -> OpenAI:
        return OpenAI(
            api_key="fake-test-key",
            max_retries=max_retries,
            timeout=timeout,
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )

    monkeypatch.setattr(analysis_worker, "OpenAI", model_client)

    def held_article_statuses() -> dict[str, str]:
        return {
            row.article_url: row.status
            for row in storage.list_digest(storage.DigestView.HELD)
            if isinstance(row, storage.DigestArticle)
        }

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(analysis_worker.run_worker, stop)
        try:
            assert first_two_running.wait(10)
            storage.save_hacker_news_articles([article, unreadable])
            queued_articles = {article.article_url: "queued", unreadable.article_url: "queued"}
            _wait_until(lambda: held_article_statuses() == queued_articles)
            assert not third_started.wait(1.5)
            assert not publisher_reads
            assert {version: analysis.status for version, analysis in storage.list_page_analyses(page.id).items()} == {
                "2": "queued",
                "3": "queued",
                "4": "queued",
            }
            storage.save_analysis_quality(analysis_worker.AnalysisQuality.HIGHER_QUALITY)
            release.set()
            _wait_until(
                lambda: all(analysis.status != "queued" for analysis in storage.list_page_analyses(page.id).values())
            )
            _wait_until(lambda: sum(workflow.status == "SUCCESS" for workflow in DBOS.list_workflows()) == 5)
            assert {workflow.workflow_id for workflow in DBOS.list_workflows()} == {
                f"page-analysis:{page.id}:{version}" for version in (2, 3, 4)
            } | {
                f"article-analysis:{sha256(story.article_url.encode()).hexdigest()}" for story in (article, unreadable)
            }
        finally:
            release.set()
            stop.set()
            worker.result(timeout=15)

    analyses = storage.list_page_analyses(page.id)
    assert peak == 2
    assert handoffs == {2: 2, 3: 1, 4: 1}
    assert attempts == {2: 2, 3: 1, 4: 3, "article": 2}
    assert publisher_reads == {article.article_url: 2, unreadable.article_url: 1}
    assert held_article_statuses() == {unreadable.article_url: "failed"}
    relevant_articles = [row for row in storage.list_digest() if isinstance(row, storage.DigestArticle)]
    assert [(row.article_url, row.status, row.summary, row.relevant_teams) for row in relevant_articles] == [
        (article.article_url, "completed", publisher_text, ["product"])
    ]
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
    assert storage.list_unqueued_changes() == []
    assert storage.list_unqueued_articles() == []
    assert app_db.with_name(f"{app_db.name}.analysis.sqlite3").is_file()
    captured = capfd.readouterr()
    assert "private provider error text" not in captured.out + captured.err
    assert "test-model" not in captured.out + captured.err
    assert "test-alternate-model" not in captured.out + captured.err


def test_evidence_keeps_an_edit_in_the_middle_of_a_long_page() -> None:
    padding = "Unchanged text\n" * 2000
    change = storage.ChangeToAnalyze(
        before=f"{padding}Old price $10{padding}",
        after=f"{padding}New price $20{padding}",
    )

    evidence = analysis_worker._change_evidence(change)

    assert "Old price $10" in evidence["before"][0]
    assert "New price $20" in evidence["after"][0]
    assert sum(len(passage) for passages in evidence.values() for passage in passages) <= 24_000


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
        html = b"<header><p>Header</p></header><article><p>New <em>sensor</em> released &amp; tested.</p></article>"
        return httpx2.Response(
            status, headers={"Content-Type": "text/html", "Location": "/another"}, stream=httpx2.ByteStream(html)
        )

    monkeypatch.setattr(article_content.socket, "getaddrinfo", resolve)
    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(article_content.httpx2, "Client", partial(httpx2.Client, transport=transport))

    evidence = article_content.read_publisher_text("https://publisher.test/story?edition=2#section")

    assert evidence == ("New sensor released & tested." if status == HTTPStatus.OK else None)


@pytest.mark.parametrize(
    "url",
    [
        "https://publisher.test:8443/story",
        "https://user@publisher.test/story",
        "http://127.0.0.1/",
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


def _model_response(*, summary: str, quote: str, marketing_quote: str | None = None) -> httpx2.Response:
    marketing = {"relevant": marketing_quote is not None, "reason": "A customer is quoted.", "quote": marketing_quote}
    result = {
        "summary": summary,
        "marketing": marketing,
        "research_and_development": {"relevant": True, "reason": "A new release was listed.", "quote": summary},
        "product": {"relevant": True, "reason": "An updated release is described.", "quote": quote},
    }
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
