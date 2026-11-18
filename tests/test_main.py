import asyncio
import json
import sqlite3
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from unittest.mock import Mock

import httpx2
import pytest
from fastapi.testclient import TestClient

from signalwatch import hacker_news, storage
from signalwatch.analysis_models import ChangeAnalysisResult, TeamDecision, TeamInterests
from signalwatch.catchup import catchup_lifespan
from signalwatch.main import app

_WEBHOOK = "/api/webhooks/changedetection"
_WEBHOOK_HEADERS = {"Authorization": "Basic " + b64encode(b"signalwatch:test-webhook-secret").decode("ascii")}
_HISTORY_GAP_HEADER = "X-SignalWatch-History-Gap"
_WEBHOOK_SETUP_URL = "https://hooks.example.com/api/webhooks/changedetection"
_WEBHOOK_SETUP_SECRET = "test-webhook-secret"
_WATCH_NOTIFICATIONS = {
    "notification_muted": False,
    "notification_urls": ["jsons://hooks.example.com/api/webhooks/changedetection"],
    "notification_format": "text",
    "notification_body": "{{watch_uuid}}",
}
_WATCH_ID = "520e831c-6d74-4c6d-96c6-c782061b0249"


@pytest.fixture(autouse=True)
def disable_scheduled_catchup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_CATCHUP_INTERVAL_SECONDS", "0")


def test_serves_frontend_and_api_docs() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == HTTPStatus.OK
        assert page.headers["content-type"].startswith("text/html")
        assert "<title>SignalWatch</title>" in page.text

        docs = client.get("/docs")
        assert docs.status_code == HTTPStatus.OK
        assert "Swagger UI" in docs.text


def test_maintainer_analysis_quality_persists_and_rejects_unknown_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    storage.save_competitor("Example", "https://example.com/")
    settings_url = "/api/maintainer/analysis"
    higher_quality = {"quality": "higher_quality"}
    with TestClient(app) as client:
        assert client.get(settings_url).json() == {"quality": "economy"}
        saved = client.put(settings_url, json=higher_quality)
        assert saved.status_code == HTTPStatus.OK
        assert saved.json() == higher_quality
    with TestClient(app) as client:
        assert client.get(settings_url).json() == higher_quality
        rejected = client.put(settings_url, json={"quality": "unsupported"})
        assert rejected.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
        assert client.get(settings_url).json() == higher_quality


def test_team_interests_default_trim_and_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(database_path))
    settings_url = "/api/team-interests"
    expected = {
        "marketing": "Pricing",
        "product": "",
        "research_and_development": "x" * 1000,
        "business_context": "b" * 2000,
    }
    with TestClient(app) as client:
        defaults = client.get(settings_url)
        assert defaults.status_code == HTTPStatus.OK
        assert defaults.json() == dict.fromkeys(expected, "")

        with sqlite3.connect(database_path) as database:
            database.execute("ALTER TABLE team_interests DROP COLUMN business_context")
            database.execute("INSERT INTO team_interests VALUES (1, 'Positioning', 'Pricing', 'APIs')")
        assert client.get(settings_url).json() == {
            "marketing": "Positioning",
            "product": "Pricing",
            "research_and_development": "APIs",
            "business_context": "",
        }

        saved = client.put(settings_url, json={key: f"  {value}\n" for key, value in expected.items()})
        assert saved.status_code == HTTPStatus.OK
        assert saved.json() == expected

        for field, maximum in (("marketing", 1000), ("business_context", 2000)):
            rejected = client.put(settings_url, json={**expected, field: "y" * (maximum + 1)})
            assert rejected.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert storage.get_team_interests().model_dump() == expected
    with TestClient(app) as client:
        assert client.get(settings_url).json() == expected


def test_digest_returns_recent_edits_across_linked_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    first = storage.save_competitor("First", "https://first.example/").pages[0]
    second = storage.save_competitor("Second", "https://second.example/").pages[0]
    unlinked = storage.save_competitor("Unlinked", "https://unlinked.example/").pages[0]
    hn_front_page = storage.save_page(first.competitor_id, "https://news.ycombinator.com/")
    for page in (first, second, unlinked, hn_front_page):
        storage.link_watch(page.id, f"watch-{page.id}")
    for page, version, before in (
        (first, 9, "old snapshot"),
        (second, 10, "old snapshot"),
        (first, 10, "old snapshot"),
        (second, 8, "old snapshot"),
        (first, 99, None),
        (unlinked, 100, "old snapshot"),
        (hn_front_page, 11, "old snapshot"),
    ):
        assert storage.save_page_version(
            page_id=page.id, watch_id=f"watch-{page.id}", version=version, before=before, after="new snapshot"
        )
    storage.record_watch_error(page_id=unlinked.id, state=storage.PageState.PENDING, error="Watch unavailable")
    marketing = TeamDecision(relevant=True, reason="Pricing changed", quote="new snapshot")
    product = TeamDecision(relevant=False, reason="No plan changes", quote=None)
    research = TeamDecision(relevant=True, reason="API terms changed", quote=None)
    storage.save_change_analysis(
        first.id,
        10,
        ChangeAnalysisResult(
            summary="Pricing changed.", marketing=marketing, product=product, research_and_development=research
        ),
    )
    storage.mark_analysis_queued(second.id, 10)
    storage.save_change_analysis(second.id, 8, None)
    hn_analysis = ChangeAnalysisResult(
        summary="Front-page rankings changed.", marketing=marketing, product=product, research_and_development=research
    )
    storage.save_change_analysis(hn_front_page.id, 11, hn_analysis)
    storage.save_change_analysis(
        first.id,
        9,
        ChangeAnalysisResult(
            summary="Formatting changed.", marketing=product, product=product, research_and_development=product
        ),
    )
    storage.save_team_interests(TeamInterests(product="New feature announcements"))

    with TestClient(app) as client:
        response = client.get("/api/digest")
        held = client.get("/api/digest", params={"view": "held"})
        hn_changes = client.get(f"/api/pages/{hn_front_page.id}/changes")
        other_changes = client.get(f"/api/pages/{first.id}/changes")
        history = client.get(f"/api/pages/{hn_front_page.id}/analysis")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {
            "page_id": first.id,
            "version": "10",
            "source_name": "First",
            "page_url": first.url,
            "status": "completed",
            "summary": "Pricing changed.",
            "relevant_teams": ["marketing", "research_and_development"],
            "team_reasons": {"marketing": "Pricing changed", "research_and_development": "API terms changed"},
        },
    ]
    assert held.status_code == HTTPStatus.OK
    assert held.json() == [
        {
            "page_id": hn_front_page.id,
            "version": "11",
            "source_name": "First",
            "page_url": hn_front_page.url,
            "status": "completed",
            "summary": "Front-page rankings changed.",
            "relevant_teams": [],
            "team_reasons": {},
            "hold_reason": "discovery_only",
        },
        {
            "page_id": second.id,
            "version": "10",
            "source_name": "Second",
            "page_url": second.url,
            "status": "queued",
            "summary": None,
            "relevant_teams": [],
            "team_reasons": {},
        },
        {
            "page_id": first.id,
            "version": "9",
            "source_name": "First",
            "page_url": first.url,
            "status": "completed",
            "summary": "Formatting changed.",
            "relevant_teams": [],
            "team_reasons": {},
        },
        {
            "page_id": second.id,
            "version": "8",
            "source_name": "Second",
            "page_url": second.url,
            "status": "failed",
            "summary": None,
            "relevant_teams": [],
            "team_reasons": {},
        },
    ]

    assert hn_changes.status_code == other_changes.status_code == HTTPStatus.OK
    assert hn_changes.headers["X-SignalWatch-Hold-Reason"] == "discovery_only"
    assert "X-SignalWatch-Hold-Reason" not in other_changes.headers
    assert hn_changes.json() == [{"version": "11", "before": "old snapshot", "after": "new snapshot"}]
    assert history.json()["11"] == {"status": "completed", "result": hn_analysis.model_dump()}


def test_hacker_news_sync_seeds_then_adds_each_publisher_link_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    source = storage.save_competitor("HN", "https://news.ycombinator.com/")
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    assert storage.save_page_version(
        page_id=page.id, watch_id=_WATCH_ID, version=4, before="Old page text", after="New page text"
    )
    old_item = "<item><title>Already on HN</title><link>https://first.example/archive</link></item>"
    new_item = (
        "<item><title>New &amp; useful</title>"
        "<link>https://publisher.example/article?ref=hn&amp;edition=2</link></item>"
    )
    failed_item = "<item><title>Publisher unavailable</title><link>https://publisher.example/failed</link></item>"
    skipped_items = (
        "<item><title>Discussion</title><link>https://news.ycombinator.com/item?id=123</link></item>"
        "<item><title>Unsafe scheme</title><link>javascript:alert(1)</link></item>"
        "<item><title>Private host</title><link>http://127.0.0.1/private</link></item>"
        "<item><title>No article link</title></item>"
    )
    first_feed = f"<rss><channel>{old_item}</channel></rss>"
    next_feed = f"<rss><channel>{old_item}{new_item}{new_item}{failed_item}{skipped_items}</channel></rss>"
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE),
            httpx2.Response(HTTPStatus.OK, text="<rss><channel/></rss>"),
            httpx2.Response(HTTPStatus.OK, text=first_feed),
            httpx2.Response(HTTPStatus.OK, text=next_feed),
            httpx2.Response(HTTPStatus.OK, text=next_feed),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(storage, "time", lambda: 10)
    article_url = "https://publisher.example/article?ref=hn&edition=2"
    failed_url = "https://publisher.example/failed"
    old_url = "https://first.example/archive"
    with TestClient(app) as client:
        for _ in range(3):
            hacker_news.sync_feed()
        baseline = client.get("/api/digest", params={"view": "held"})
        assert baseline.status_code == HTTPStatus.OK
        edits = baseline.json()
        assert len(edits) == 1 and edits[0]["version"] == "4"
        assert storage.list_unqueued_articles() == []
        assert storage.get_hacker_news_article(old_url) is None

        hacker_news.sync_feed()
        monkeypatch.setattr(storage, "time", lambda: 20)
        hacker_news.sync_feed()
        assert storage.list_unqueued_articles(limit=1) == [article_url]
        assert storage.list_unqueued_articles() == [article_url, failed_url]
        assert storage.get_hacker_news_article(article_url) == storage.HackerNewsArticle("New & useful", article_url)
        article = {
            "kind": "article",
            "source_name": "Hacker News",
            "title": "New & useful",
            "article_url": article_url,
            "version": "10",
            "status": "pending",
            "summary": None,
            "relevant_teams": [],
            "team_reasons": {},
        }
        failed = {**article, "article_url": failed_url, "title": "Publisher unavailable", "status": "failed"}
        storage.save_article_analysis(failed_url, None)
        assert client.get("/api/digest", params={"view": "held"}).json() == [article, failed, *edits]
        assert client.get("/api/digest").json() == []
        storage.mark_article_analysis_queued(article_url)
        assert storage.list_unqueued_articles() == []
        assert client.get("/api/digest", params={"view": "held"}).json() == [
            {**article, "status": "queued"},
            failed,
            *edits,
        ]
        held = TeamDecision(relevant=False, reason="No match", quote=None)
        relevant = TeamDecision(relevant=True, reason="Matches Product's interests", quote="Publisher evidence")
        storage.save_article_analysis(
            article_url,
            ChangeAnalysisResult(
                summary="A new release.", marketing=held, product=relevant, research_and_development=held
            ),
        )
        storage.save_article_analysis(article_url, None)
        assert client.get("/api/digest").json() == [
            {
                **article,
                "status": "completed",
                "summary": "A new release.",
                "relevant_teams": ["product"],
                "team_reasons": {"product": relevant.reason},
            },
        ]
        assert client.get("/api/digest", params={"view": "held"}).json() == [failed, *edits]
        storage.save_article_analysis(
            failed_url,
            ChangeAnalysisResult(
                summary="No relevant changes.", marketing=held, product=held, research_and_development=held
            ),
        )
        assert [entry["article_url"] for entry in client.get("/api/digest").json()] == [article_url]
        assert client.get("/api/digest", params={"view": "held"}).json() == [
            {**failed, "status": "completed", "summary": "No relevant changes."},
            *edits,
        ]

        assert client.delete(f"/api/competitors/{source.id}").status_code == HTTPStatus.NO_CONTENT
        hacker_news.sync_feed()
        assert client.get("/api/digest").json() == []
        assert client.get("/api/digest", params={"view": "held"}).json() == edits

    assert handler.call_count == 5
    for call in handler.call_args_list:
        request = call.args[0]
        assert request.method == HTTPMethod.GET
        assert str(request.url) == "https://news.ycombinator.com/rss"
        assert "authorization" not in request.headers and "x-api-key" not in request.headers


def test_catchup_imports_versions_on_startup_and_timer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNALWATCH_CATCHUP_INTERVAL_SECONDS", "60")
    source = storage.save_competitor("Example", "https://example.com/")
    failed_page = source.pages[0]
    page = storage.save_page(source.id, "https://example.com/watched")
    storage.link_watch(failed_page.id, "a655d37b-0928-4a73-aa9f-031411c407bc")
    storage.link_watch(page.id, _WATCH_ID)
    history_path = f"/api/v1/watch/{_WATCH_ID}/history"
    first_change_read = Event()
    complete_first_change = Event()
    new_version = Event()

    def handle(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == history_path:
            versions = {"1": "a.txt", "2": "b.txt", "3": "c.txt"}
            if new_version.is_set():
                versions["4"] = "d.txt"
            return httpx2.Response(HTTPStatus.OK, json=versions)
        if path.startswith(f"{history_path}/"):
            version = path.rsplit("/", 1)[1]
            if version == "2":
                first_change_read.set()
                assert complete_first_change.wait(5)
            return httpx2.Response(HTTPStatus.OK, text={"1": "A", "2": "B", "3": "C", "4": "D"}[version])
        assert path == "/api/v1/watch/a655d37b-0928-4a73-aa9f-031411c407bc/history"
        return httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE)

    async def interrupt_startup() -> None:
        async with catchup_lifespan(app):
            try:
                assert await asyncio.to_thread(first_change_read.wait, 5)
            finally:
                asyncio.get_running_loop().call_soon(complete_first_change.set)

    def wait_for_changes(client: TestClient, expected: list[dict[str, str]]) -> None:
        deadline = monotonic() + 5
        while (actual := client.get(f"/api/pages/{page.id}/changes").json()) != expected and monotonic() < deadline:
            sleep(0.02)
        assert actual == expected

    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handle)))
    baseline = {"version": "2", "before": "A", "after": "B"}
    asyncio.run(interrupt_startup())
    assert [change.model_dump() for change in storage.list_page_changes(page.id)] == [baseline]

    monkeypatch.setenv("SIGNALWATCH_CATCHUP_INTERVAL_SECONDS", "1")
    with TestClient(app) as client:
        recovered = {"version": "3", "before": "B", "after": "C"}
        wait_for_changes(client, [recovered, baseline])
        new_version.set()
        wait_for_changes(client, [{"version": "4", "before": "C", "after": "D"}, recovered, baseline])


def test_changedetection_webhook_saves_one_change_for_duplicate_deliveries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", "test-webhook-secret")
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    engine_path = f"/api/v1/watch/{_WATCH_ID}"
    history = {"10": "previous.txt"}
    deliver_together = Barrier(2, timeout=5)
    reading_snapshot = Event()
    overlapping_history = Event()
    snapshot_reads: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["x-api-key"] == "test-api-key"
        if request.method == HTTPMethod.DELETE and request.url.path == engine_path:
            return httpx2.Response(HTTPStatus.NO_CONTENT)
        assert request.method == HTTPMethod.GET
        match request.url.path:
            case path if path == f"{engine_path}/history":
                if reading_snapshot.is_set():
                    overlapping_history.set()
                return httpx2.Response(HTTPStatus.OK, json=history)
            case path if path == f"{engine_path}/history/10":
                snapshot_reads.append("10")
                return httpx2.Response(HTTPStatus.OK, text="Price: $10")
            case path if path == f"{engine_path}/history/11":
                snapshot_reads.append("11")
                reading_snapshot.set()
                assert not overlapping_history.wait(0.25)
                reading_snapshot.clear()
                return httpx2.Response(HTTPStatus.OK, text="Price: $15")
        raise AssertionError(f"Unexpected snapshot request: {request.url.path}")

    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handle)))
    payload = {"title": "Watch changed", "message": _WATCH_ID, "version": "1.1"}
    changes_url = f"/api/pages/{page.id}/changes"

    def deliver(client: TestClient) -> int:
        deliver_together.wait()
        return client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code

    with TestClient(app) as client:
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        first_import = client.get(changes_url)
        assert first_import.json() == []
        assert _HISTORY_GAP_HEADER not in first_import.headers

    history = {"11": "latest.txt"}
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(deliver, client) for _ in range(2)]
            assert [call.result() for call in calls] == [HTTPStatus.NO_CONTENT, HTTPStatus.NO_CONTENT]
        assert snapshot_reads == ["10", "11"]
        recovered = client.get(changes_url)
        assert recovered.json() == [{"version": "11", "before": "Price: $10", "after": "Price: $15"}]
        assert recovered.headers[_HISTORY_GAP_HEADER] == "true"

        assert client.delete(f"/api/pages/{page.id}").status_code == HTTPStatus.NO_CONTENT
        assert client.get(changes_url).status_code == HTTPStatus.NOT_FOUND
    with sqlite3.connect(db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM page_changes").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM history_gaps").fetchone() == (0,)


@pytest.mark.parametrize("empty_history", [False, True])
def test_changedetection_webhook_catches_up_in_order_and_resumes_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_history: bool
) -> None:
    db_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", "test-webhook-secret")
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    with sqlite3.connect(db_path) as database:
        database.execute("DROP TABLE page_changes")
        database.execute("""CREATE TABLE page_changes (
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            version TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, PRIMARY KEY (page_id, version))""")
        database.execute("INSERT INTO page_changes VALUES (?, '2', 'A', 'B')", (page.id,))
    retained_history = {"11": "e.txt", "10": "d.txt"}
    empty_listing = [httpx2.Response(HTTPStatus.OK, json={})] if empty_history else []
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE),
            httpx2.Response(HTTPStatus.OK, json=[]),
            httpx2.Response(HTTPStatus.OK, json={"10": "d.txt", "3": "c.txt", "2": "b.txt"}),
            httpx2.Response(HTTPStatus.OK, text="C"),
            httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE),
            *empty_listing,
            httpx2.Response(HTTPStatus.OK, json=retained_history),
            httpx2.Response(HTTPStatus.OK, text="D"),
            httpx2.Response(HTTPStatus.OK, text="E"),
            httpx2.Response(HTTPStatus.OK, json=retained_history),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))
    payload = {"message": _WATCH_ID}
    changes_url = f"/api/pages/{page.id}/changes"
    with TestClient(app) as client:
        first_import = client.get(changes_url)
        assert first_import.json() == [{"version": "2", "before": "A", "after": "B"}]
        assert _HISTORY_GAP_HEADER not in first_import.headers
        for _ in range(2):
            assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.BAD_GATEWAY
            unchanged = client.get(changes_url)
            assert unchanged.json() == first_import.json()
            assert _HISTORY_GAP_HEADER not in unchanged.headers

        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.BAD_GATEWAY
        completed = [{"version": "3", "before": "B", "after": "C"}, {"version": "2", "before": "A", "after": "B"}]
        partial_import = client.get(changes_url)
        assert partial_import.json() == completed
        assert _HISTORY_GAP_HEADER not in partial_import.headers

        if empty_history:
            assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
            no_new_changes = client.get(changes_url)
            assert no_new_changes.json() == completed
            assert no_new_changes.headers[_HISTORY_GAP_HEADER] == "true"

        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        expected = [{"version": "11", "before": "D", "after": "E"}, {"version": "10", "before": "C", "after": "D"}]
        recovered = client.get(changes_url)
        assert recovered.json() == expected + completed
        assert recovered.headers[_HISTORY_GAP_HEADER] == "true"

    with TestClient(app) as client:
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        repeated = client.get(changes_url)
        assert repeated.json() == expected + completed
        assert repeated.headers[_HISTORY_GAP_HEADER] == "true"

    history_path = f"/api/v1/watch/{_WATCH_ID}/history"
    assert [call.args[0].url.path.removeprefix(history_path) for call in handler.call_args_list] == [
        "",
        "",
        "",
        "/3",
        "/10",
        *([""] if empty_history else []),
        "",
        "/10",
        "/11",
        "",
    ]
    with sqlite3.connect(db_path) as database:
        rows = database.execute("SELECT version FROM page_changes WHERE page_id = ? ORDER BY version", (page.id,))
        assert rows.fetchall() == [(2,), (3,), (10,), (11,)]


def test_changedetection_webhook_rejects_invalid_access_before_engine_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.delenv("CHANGEDETECTION_WEBHOOK_SECRET", raising=False)
    handler = Mock()
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))
    payload = {"message": _WATCH_ID}
    with TestClient(app) as client:
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NOT_FOUND
        monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", "test-webhook-secret")
        assert client.post(_WEBHOOK, json=payload).status_code == HTTPStatus.UNAUTHORIZED
        assert client.post(_WEBHOOK, json=payload, auth=("signalwatch", "wrong-secret")).status_code == (
            HTTPStatus.UNAUTHORIZED
        )
        unknown_watch = client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS)
        assert unknown_watch.status_code == HTTPStatus.NOT_FOUND
    handler.assert_not_called()


def test_watch_creation_requires_webhook_setup_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.delenv("SIGNALWATCH_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", _WEBHOOK_SETUP_SECRET)
    source = storage.save_competitor("Example", "https://example.com/")
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.CREATED, json={"uuid": _WATCH_ID}),
            httpx2.Response(HTTPStatus.OK, json={"url": "https://example.com/news", **_WATCH_NOTIFICATIONS}),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))
    settings_url = "/api/maintainer/webhooks"
    pages_url = f"/api/competitors/{source.id}/pages"
    with TestClient(app) as client:
        assert client.get(settings_url).json() == {"configured": False}
        assert client.put(settings_url, json={"enabled": True}).status_code == HTTPStatus.METHOD_NOT_ALLOWED
        added = client.post(pages_url, json={"url": "https://example.com/news"})
        assert added.status_code == HTTPStatus.CREATED
        pending = added.json()
        assert pending["state"] == "pending" and pending["watch_id"] is None
        assert "webhook URL and secret" in pending["error"]
        handler.assert_not_called()

        monkeypatch.setenv("SIGNALWATCH_WEBHOOK_URL", _WEBHOOK_SETUP_URL)
        monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", "bad secret%20")
        assert client.get(settings_url).json() == {"configured": False}
        monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", _WEBHOOK_SETUP_SECRET)
        assert client.get(settings_url).json() == {"configured": True}
        retried = client.post(f"/api/pages/{pending['id']}/connect")
        assert retried.status_code == HTTPStatus.OK and retried.json()["watch_id"] == _WATCH_ID
        assert _WEBHOOK_SETUP_SECRET not in retried.text and _WEBHOOK_SETUP_URL not in retried.text

    calls = [call.args[0] for call in handler.call_args_list]
    assert [request.method for request in calls] == [HTTPMethod.POST, HTTPMethod.GET]
    assert json.loads(calls[0].content) == {
        "url": "https://example.com/news",
        "title": "Example",
        "fetch_backend": "html_requests",
        **_WATCH_NOTIFICATIONS,
    }


def test_configuration_persists_and_links_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNALWATCH_WEBHOOK_URL", _WEBHOOK_SETUP_URL)
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", _WEBHOOK_SETUP_SECRET)
    watch_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    extra_watch_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.CREATED, json={"uuid": watch_id}),
            httpx2.Response(HTTPStatus.OK, json={"url": "https://example.com/", **_WATCH_NOTIFICATIONS}),
            httpx2.Response(
                HTTPStatus.OK, json={"paused": False, "last_checked": 0, "check_count": 0, "last_error": False}
            ),
            httpx2.Response(
                HTTPStatus.OK,
                json={
                    "paused": False,
                    "last_checked": 123,
                    "check_count": 0,
                    "last_error": False,
                    "headers": {"Authorization": "private-value"},
                },
            ),
            httpx2.Response(HTTPStatus.CREATED, json={"uuid": extra_watch_id}),
            httpx2.Response(HTTPStatus.OK, json={"url": "https://example.com/pricing", **_WATCH_NOTIFICATIONS}),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        created = client.post("/api/competitors", json={"name": "Example", "website": "https://example.com/"})
        assert created.status_code == HTTPStatus.CREATED
        competitor = created.json()
        page = competitor["pages"][0]
        assert page["watch_id"] == watch_id
        assert page["state"] == "linked"
        assert client.get(f"/api/pages/{page['id']}/status").json()["last_checked"] is None
        status = client.get(f"/api/pages/{page['id']}/status")
        assert status.json() == {
            "available": True,
            "paused": False,
            "last_checked": 123,
            "check_count": 0,
            "error": None,
        }
        added = client.post(f"/api/competitors/{competitor['id']}/pages", json={"url": "https://example.com/pricing"})
        assert added.status_code == HTTPStatus.CREATED
        extra_page = added.json()
        assert extra_page["competitor_id"] == competitor["id"]
        assert extra_page["watch_id"] == extra_watch_id
        assert extra_page["state"] == "linked"
        competitor["pages"].append(extra_page)

    with TestClient(app) as client:
        assert client.get("/api/competitors").json() == [competitor]
        assert client.post(f"/api/pages/{page['id']}/connect").json() == page
        assert client.post(f"/api/pages/{extra_page['id']}/connect").json() == extra_page

    methods = [call.args[0].method for call in handler.call_args_list]
    assert methods == [HTTPMethod.POST, HTTPMethod.GET, HTTPMethod.GET, HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.GET]
    assert handler.call_args_list[1].args[0].url.path == f"/api/v1/watch/{watch_id}"
    assert handler.call_args_list[5].args[0].url.path == f"/api/v1/watch/{extra_watch_id}"
    request = handler.call_args_list[0].args[0]
    assert request.headers["x-api-key"] == "test-api-key"
    assert json.loads(request.content) == {
        "url": "https://example.com/",
        "title": "Example",
        "fetch_backend": "html_requests",
        **_WATCH_NOTIFICATIONS,
    }
    assert json.loads(handler.call_args_list[4].args[0].content) == {
        "url": "https://example.com/pricing",
        "title": "Example",
        "fetch_backend": "html_requests",
        **_WATCH_NOTIFICATIONS,
    }


def test_unsafe_target_is_rejected_before_engine_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    handler = Mock()
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        rejected = client.post("/api/competitors", json={"name": "Unsafe target", "website": "https://127.0.0.1/"})
        assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert client.get("/api/competitors").json() == []

    handler.assert_not_called()


def test_creation_retry_stops_after_uncertain_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNALWATCH_WEBHOOK_URL", _WEBHOOK_SETUP_URL)
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", _WEBHOOK_SETUP_SECRET)
    handler = Mock(
        side_effect=[
            httpx2.ConnectError("The engine connection failed."),
            httpx2.ReadTimeout("The create response timed out."),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        created = client.post("/api/competitors", json={"name": "Example", "website": "https://example.com/"})
        assert created.status_code == HTTPStatus.CREATED
        page = created.json()["pages"][0]
        assert page["state"] == "pending"
        assert page["watch_id"] is None
        assert page["error"]

    with TestClient(app) as client:
        assert client.get("/api/competitors").json()[0]["pages"] == [page]
        retried = client.post(f"/api/pages/{page['id']}/connect")
        assert retried.status_code == HTTPStatus.OK
        page = retried.json()
        assert page["state"] == "attention"
        assert page["watch_id"] is None
        assert page["error"]
        assert "test-api-key" not in retried.text

    with TestClient(app) as client:
        assert client.get("/api/competitors").json()[0]["pages"] == [page]
        assert client.post(f"/api/pages/{page['id']}/connect").status_code == HTTPStatus.CONFLICT

    assert [call.args[0].method for call in handler.call_args_list] == [HTTPMethod.POST, HTTPMethod.POST]


def test_delete_page_persists_and_preserves_other_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(database_path))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    watch_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    with sqlite3.connect(database_path) as database:
        database.executescript("""
            CREATE TABLE competitors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, website TEXT NOT NULL);
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY, competitor_id INTEGER NOT NULL REFERENCES competitors(id), url TEXT NOT NULL,
                watch_id TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT, attempted_at TEXT,
                UNIQUE (competitor_id, url)
            );
            INSERT INTO competitors VALUES (1, 'Example', 'https://example.com/');
            INSERT INTO pages (competitor_id, url) VALUES (1, 'https://example.com/');
        """)
        database.execute(
            "INSERT INTO pages (competitor_id, url, watch_id, state) VALUES (?, ?, ?, ?)",
            (1, "https://example.com/pricing", watch_id, storage.PageState.LINKED),
        )
    competitor = storage.list_competitors()[0]
    other_page, page = competitor.pages
    handler = Mock(return_value=httpx2.Response(HTTPStatus.NO_CONTENT))
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        deleted = client.delete(f"/api/pages/{page.id}")
        assert deleted.status_code == HTTPStatus.NO_CONTENT
        assert deleted.content == b""

    with TestClient(app) as client:
        competitor.pages = [other_page]
        assert client.get("/api/competitors").json() == [competitor.model_dump(mode="json")]
        replacement = storage.save_page(competitor.id, "https://example.com/new")
        replacement_watch_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
        storage.link_watch(replacement.id, replacement_watch_id)
        assert replacement.id > page.id
        assert client.delete(f"/api/pages/{page.id}").status_code == HTTPStatus.NO_CONTENT
        competitor.pages.append(storage.get_page(replacement.id))
        assert client.get("/api/competitors").json() == [competitor.model_dump(mode="json")]
        assert storage.get_page(replacement.id).watch_id == replacement_watch_id

    handler.assert_called_once()
    request = handler.call_args.args[0]
    assert request.method == HTTPMethod.DELETE
    assert request.url.path == f"/api/v1/watch/{watch_id}"


def test_delete_missing_watch_preserves_same_url_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    competitor = storage.save_competitor("Original", "https://example.com/")
    replacement = storage.save_competitor("Replacement", competitor.website)
    page = competitor.pages[0]
    watch_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    replacement_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
    storage.link_watch(page.id, watch_id)
    storage.link_watch(replacement.pages[0].id, replacement_id)
    replacement.pages = [storage.get_page(replacement.pages[0].id)]
    handler = Mock(
        return_value=httpx2.Response(
            HTTPStatus.BAD_REQUEST, json={"message": f"No watch exists with the UUID of {watch_id}"}
        )
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        deleted = client.delete(f"/api/pages/{page.id}")
        assert deleted.status_code == HTTPStatus.NO_CONTENT
        assert deleted.content == b""
        competitor.pages = []
        assert client.get("/api/competitors").json() == [
            competitor.model_dump(mode="json"),
            replacement.model_dump(mode="json"),
        ]

    handler.assert_called_once()
    request = handler.call_args.args[0]
    assert request.method == HTTPMethod.DELETE
    assert request.url.path == f"/api/v1/watch/{watch_id}"
    assert replacement_id not in str(request.url)


def test_delete_page_engine_failure_retains_page_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    competitor = storage.save_competitor("Example", "https://example.com/")
    page = competitor.pages[0]
    watch_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    storage.link_watch(page.id, watch_id)
    competitor.pages = [storage.get_page(page.id)]
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.BAD_REQUEST, json={"message": "Other rejection: test-api-key"}),
            httpx2.Response(HTTPStatus.NO_CONTENT),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        rejected = client.delete(f"/api/pages/{page.id}")
        assert rejected.status_code == HTTPStatus.BAD_GATEWAY
        assert rejected.json() == {"detail": "The engine rejected the request (HTTP 400)."}
        assert "test-api-key" not in rejected.text
        assert client.get("/api/competitors").json() == [competitor.model_dump(mode="json")]

    with TestClient(app) as client:
        assert client.delete(f"/api/pages/{page.id}").status_code == HTTPStatus.NO_CONTENT
        competitor.pages = []
        assert client.get("/api/competitors").json() == [competitor.model_dump(mode="json")]

    assert [call.args[0].method for call in handler.call_args_list] == [HTTPMethod.DELETE, HTTPMethod.DELETE]
    assert all(call.args[0].url.path == f"/api/v1/watch/{watch_id}" for call in handler.call_args_list)


def test_delete_competitor_cleans_children_and_preserves_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    competitor = storage.save_competitor("Original", "https://example.com/")
    extra = storage.save_page(competitor.id, "https://example.com/pricing")
    unrelated = storage.save_competitor("Unrelated", competitor.website)
    first_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    second_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
    storage.link_watch(competitor.pages[0].id, first_id)
    storage.link_watch(extra.id, second_id)
    storage.link_watch(unrelated.pages[0].id, "bcb36d70-c935-4ae0-8424-c20811ae8196")
    unrelated.pages = [storage.get_page(unrelated.pages[0].id)]
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.BAD_REQUEST, json={"message": f"No watch exists with the UUID of {first_id}"}),
            httpx2.Response(HTTPStatus.NO_CONTENT),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        deleted = client.delete(f"/api/competitors/{competitor.id}")
        assert deleted.status_code == HTTPStatus.NO_CONTENT
        assert deleted.content == b""
    with TestClient(app) as client:
        assert client.get("/api/competitors").json() == [unrelated.model_dump(mode="json")]

    assert storage.get_page(competitor.pages[0].id) is None
    assert storage.get_page(extra.id) is None
    assert [call.args[0].method for call in handler.call_args_list] == [HTTPMethod.DELETE, HTTPMethod.DELETE]
    assert [call.args[0].url.path for call in handler.call_args_list] == [
        f"/api/v1/watch/{first_id}",
        f"/api/v1/watch/{second_id}",
    ]


def test_delete_empty_competitor_preserves_new_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(database_path))
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE competitors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, website TEXT NOT NULL)")
        database.execute("INSERT INTO competitors VALUES (1, 'Original', 'https://example.com/')")
    handler = Mock()
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        deleted = client.delete("/api/competitors/1")
        assert deleted.status_code == HTTPStatus.NO_CONTENT
        assert deleted.content == b""
        replacement = storage.save_competitor("Replacement", "https://example.org/")
        assert replacement.id > 1
        assert client.delete("/api/competitors/1").status_code == HTTPStatus.NO_CONTENT
        assert client.get("/api/competitors").json() == [replacement.model_dump(mode="json")]

    handler.assert_not_called()


def test_delete_competitor_blocks_creation_then_retries_partial_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    competitor = storage.save_competitor("Example", "https://example.com/")
    extra = storage.save_page(competitor.id, "https://example.com/pricing")
    first_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    second_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
    storage.link_watch(competitor.pages[0].id, first_id)
    assert storage.claim_watch_creation(extra) == competitor.name
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.NO_CONTENT),
            httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE, json={"message": "private test-api-key failure"}),
            httpx2.Response(HTTPStatus.BAD_REQUEST, json={"message": f"No watch exists with the UUID of {first_id}"}),
            httpx2.Response(HTTPStatus.NO_CONTENT),
        ]
    )
    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handler)))

    with TestClient(app) as client:
        blocked = client.delete(f"/api/competitors/{competitor.id}")
        assert blocked.status_code == HTTPStatus.CONFLICT
        assert storage.get_page(extra.id).state == storage.PageState.CREATING
        handler.assert_not_called()
        storage.link_watch(extra.id, second_id)
        before = client.get("/api/competitors").json()
        failed = client.delete(f"/api/competitors/{competitor.id}")
        assert failed.status_code == HTTPStatus.BAD_GATEWAY
        assert failed.json() == {"detail": "The engine rejected the request (HTTP 503)."}
        assert "test-api-key" not in failed.text
        assert client.get("/api/competitors").json() == before
    with TestClient(app) as client:
        assert client.delete(f"/api/competitors/{competitor.id}").status_code == HTTPStatus.NO_CONTENT
        assert client.get("/api/competitors").json() == []

    assert [call.args[0].method for call in handler.call_args_list] == [HTTPMethod.DELETE] * 4
    assert [call.args[0].url.path for call in handler.call_args_list] == [
        f"/api/v1/watch/{first_id}",
        f"/api/v1/watch/{second_id}",
        f"/api/v1/watch/{first_id}",
        f"/api/v1/watch/{second_id}",
    ]
