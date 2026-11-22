import asyncio
import json
import sqlite3
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from signalwatch import article_feeds, changedetection, storage
from signalwatch.analysis_models import (
    AnalysisQuality,
    ArticleAnalysisResult,
    ChangeAnalysisResult,
    TeamDecision,
    TeamInterests,
)
from signalwatch.article_content import PublicContent
from signalwatch.catchup import _catch_up_once, catchup_lifespan
from signalwatch.main import app

_WEBHOOK = "/api/webhooks/changedetection"
_WEBHOOK_HEADERS = {"Authorization": "Basic " + b64encode(b"signalwatch:test-webhook-secret").decode("ascii")}
_HISTORY_GAP_HEADER = "X-SignalWatch-History-Gap"
_ARTICLE_FEEDS = "/api/maintainer/article-feeds"
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


def test_feedback_is_private_team_specific_and_retry_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.delenv("SIGNALWATCH_FEEDBACK_ORIGIN", raising=False)
    saved_url = str(HttpUrl(f"https://example.com/{'é' * 350}"))
    assert len(saved_url) > 2083
    source = storage.save_competitor("Example", saved_url)
    page = source.pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    assert storage.save_page_version(page_id=page.id, watch_id=_WATCH_ID, version=10, before="before", after="after")
    article = storage.Article("New article", "https://EXAMPLE.com/article?ref=RSS")
    feed = storage.ArticleFeed(source.id, "https://example.com/feed.xml")
    storage.set_article_feed(source_id=feed.source_id, url=feed.url)
    storage.save_discovered_articles(feed, [])
    storage.save_discovered_articles(feed, [article])

    useful = {
        "id": str(uuid4()),
        "team": "marketing",
        "judgment": "useful",
        "url": page.url,
        "page_id": page.id,
        "version": "10",
    }
    missed = {**useful, "id": str(uuid4()), "team": "product", "judgment": "missed"}
    noisy = {
        "id": str(uuid4()),
        "team": "research_and_development",
        "judgment": "noisy",
        "url": article.article_url,
        "source_id": source.id,
    }
    missing = {
        "id": str(uuid4()),
        "team": "product",
        "judgment": "missed",
        "url": "https://example.com/not-discovered",
        "occurred_on": "2026-11-20",
        "note": "  Why it matters  ",
    }
    origin = "http://127.0.0.1:8001"
    headers = {"Origin": origin}
    with TestClient(app) as client:
        assert "X-Feedback-Enabled" not in client.get("/api/digest").headers
        assert client.get("/api/feedback").status_code == HTTPStatus.NOT_FOUND
        assert client.post("/api/feedback", json=useful, headers=headers).status_code == HTTPStatus.NOT_FOUND
        monkeypatch.setenv("SIGNALWATCH_FEEDBACK_ORIGIN", origin)
        assert client.get("/api/digest?view=held").headers["X-Feedback-Enabled"] == "true"
        assert client.post("/api/feedback", json=useful).status_code == HTTPStatus.FORBIDDEN
        wrong_origin = {"Origin": "https://other.example/"}
        assert client.post("/api/feedback", json=useful, headers=wrong_origin).status_code == HTTPStatus.FORBIDDEN
        for feedback in (useful, missed, noisy, missing, useful):
            assert client.post("/api/feedback", json=feedback, headers=headers).status_code == HTTPStatus.NO_CONTENT
        conflict = client.post("/api/feedback", json={**useful, "judgment": "noisy"}, headers=headers)
        assert conflict.status_code == HTTPStatus.CONFLICT
        unknown_version = {**useful, "id": str(uuid4()), "version": "11"}
        assert client.post("/api/feedback", json=unknown_version, headers=headers).status_code == HTTPStatus.NOT_FOUND
        for url in ("ftp://example.com/", saved_url):
            bad_url = {**missing, "url": url, "id": str(uuid4())}
            rejected = client.post("/api/feedback", json=bad_url, headers=headers)
            assert rejected.status_code == HTTPStatus.UNPROCESSABLE_CONTENT

        linked_page = storage.get_page(page.id)
        assert linked_page is not None and storage.delete_page(linked_page)
        assert client.post("/api/feedback", json=useful, headers=headers).status_code == HTTPStatus.NO_CONTENT
        response = client.get("/api/feedback")
        assert response.status_code == HTTPStatus.OK
        rows = response.json()
        assert all(row.pop("created_at").endswith("Z") for row in rows)
        assert rows == [{**missing, "note": "Why it matters"}, noisy, missed, useful]


def test_slack_feedback_links_open_saved_items_outside_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.delenv("SIGNALWATCH_FEEDBACK_ORIGIN", raising=False)
    monkeypatch.setattr(storage, "_DIGEST_LIMIT", 1)
    product = storage.DigestTeam.PRODUCT
    channels = {product: "C_ALERTS"}
    source = storage.save_competitor("Example", "https://example.com/")
    page = source.pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    held = TeamDecision(relevant=False, reason="No match", quote=None)
    shown = TeamDecision(relevant=True, reason="API change", quote="Evidence")
    analysis = ChangeAnalysisResult(
        summary="Page changed", marketing=held, product=shown, research_and_development=held
    )
    for version in (10, 11):
        assert storage.save_page_version(page_id=page.id, watch_id=_WATCH_ID, version=version,
                                         before="Old text", after="Evidence")
        storage.save_change_analysis(page.id, version, analysis, slack_channels=channels if version == 10 else None)
    page_delivery = storage.claim_slack_delivery(channels)
    assert page_delivery is not None and page_delivery.item.version == "10"
    storage.finish_slack_delivery(page_delivery.id, "sent")

    storage.save_team_interests(TeamInterests(product="APIs", business_context="Enterprise controls"))
    feed = storage.ArticleFeed(source.id, "https://example.com/feed.xml")
    storage.set_article_feed(source_id=feed.source_id, url=feed.url)
    storage.save_discovered_articles(feed, [])
    article = storage.Article("API release", "https://example.com/release?ref=feed")
    storage.save_discovered_articles(feed, [article])
    storage.save_article_analysis(source.id, article.article_url,
                                  ArticleAnalysisResult(**analysis.model_dump(), change_quote="Evidence"),
                                  slack_channels=channels)
    article_delivery = storage.claim_slack_delivery(channels)
    assert article_delivery is not None  # An uncertain send may already be visible in Slack.
    storage.save_team_interests(TeamInterests(product="APIs"))

    with TestClient(app) as client:
        page_path = f"/api/feedback/items/{page_delivery.feedback_token}"
        assert client.get(page_path).status_code == HTTPStatus.NOT_FOUND
        monkeypatch.setenv("SIGNALWATCH_FEEDBACK_ORIGIN", "http://127.0.0.1:8001")
        assert [item["version"] for item in client.get("/api/digest").json()] == ["11"]
        for delivery in (page_delivery, article_delivery):
            response = client.get(f"/api/feedback/items/{delivery.feedback_token}")
            assert response.status_code == HTTPStatus.OK and response.headers["Cache-Control"] == "no-store"
            assert response.json() == delivery.item.model_dump(mode="json")
        assert client.get(f"/api/feedback/items/{page_delivery.id}").status_code == HTTPStatus.UNPROCESSABLE_CONTENT
        storage.finish_slack_delivery(article_delivery.id, "failed")
        assert client.get(f"/api/feedback/items/{article_delivery.feedback_token}").status_code == HTTPStatus.NOT_FOUND
        storage.record_watch_error(page_id=page.id, state=storage.PageState.PENDING, error="Watch unavailable")
        assert client.get(page_path).json() == page_delivery.item.model_dump(mode="json")
        linked_page = storage.get_page(page.id)
        assert linked_page is not None and storage.delete_page(linked_page)
        assert client.get(page_path).status_code == HTTPStatus.NOT_FOUND


def test_slack_feedback_link_cannot_retarget_after_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_db = tmp_path / "configuration.sqlite3"
    backup_db = tmp_path / "backup.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(app_db))
    monkeypatch.setenv("SIGNALWATCH_FEEDBACK_ORIGIN", "http://127.0.0.1:8001")
    channels = {storage.DigestTeam.PRODUCT: "C_ALERTS"}
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    decision = TeamDecision(relevant=True, reason="API change", quote="Evidence")
    analysis = ChangeAnalysisResult(
        summary="Page changed", marketing=decision, product=decision, research_and_development=decision
    )
    for version in (10, 11):
        assert storage.save_page_version(
            page_id=page.id, watch_id=_WATCH_ID, version=version, before="Old text", after="Evidence"
        )
    with closing(sqlite3.connect(app_db)) as database, closing(sqlite3.connect(backup_db)) as backup:
        database.backup(backup)

    storage.save_change_analysis(page.id, 10, analysis, slack_channels=channels)
    with closing(sqlite3.connect(app_db)) as database, database:
        # Keep a queued delivery from before the feedback token column existed.
        database.execute("DROP INDEX slack_delivery_feedback_token")
        database.execute("ALTER TABLE slack_deliveries DROP COLUMN feedback_token")
    original = storage.claim_slack_delivery(channels)
    assert original is not None
    storage.finish_slack_delivery(original.id, "sent")
    original_path = f"/api/feedback/items/{original.feedback_token}"
    with TestClient(app) as client:
        assert client.get(original_path).json()["version"] == "10"

        monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(backup_db))
        storage.save_change_analysis(page.id, 11, analysis, slack_channels=channels)
        replacement = storage.claim_slack_delivery(channels)
        assert replacement is not None and replacement.id == original.id
        assert client.get(original_path).status_code == HTTPStatus.NOT_FOUND
        assert client.get(f"/api/feedback/items/{replacement.feedback_token}").json()["version"] == "11"


def test_feedback_suggestion_requires_evidence_and_applies_to_one_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.delenv("SIGNALWATCH_FEEDBACK_ORIGIN", raising=False)
    interests = TeamInterests(marketing="Positioning", product="APIs", business_context="Enterprise controls")
    storage.save_team_interests(interests)
    source_id = storage.save_competitor("Example", "https://example.com/").id
    feed = storage.ArticleFeed(source_id, "https://example.com/feed.xml")
    storage.set_article_feed(source_id=source_id, url=feed.url)
    storage.save_discovered_articles(feed, [])
    articles = [storage.Article(name, f"https://example.com/{name}") for name in ("miss", "shown", "held")]
    storage.save_discovered_articles(feed, articles)
    held = TeamDecision(relevant=False, reason="No match", quote=None)
    shown = TeamDecision(relevant=True, reason="A match", quote="Evidence")
    for article in articles:
        storage.save_article_analysis(
            source_id,
            article.article_url,
            ArticleAnalysisResult(
                summary=article.title,
                marketing=held,
                product=shown if article.title == "shown" else held,
                research_and_development=held,
                change_quote="Evidence",
            ),
        )

    path = "/api/feedback/suggestion"
    origin = "http://127.0.0.1:8001"
    headers = {"Origin": origin}
    team = {"team": "product"}
    contested = storage.FeedbackInput(
        id=uuid4(),
        team="product",
        judgment="noisy",
        url=articles[1].article_url,
        source_id=source_id,
    )
    storage.save_feedback(contested)
    with TestClient(app) as client:
        assert client.get(path, params=team).status_code == HTTPStatus.NOT_FOUND
        assert client.post(path, json=team, headers=headers).status_code == HTTPStatus.NOT_FOUND
        assert client.post(f"{path}/missing/apply", headers=headers).status_code == HTTPStatus.NOT_FOUND
        assert client.delete(f"{path}/missing", headers=headers).status_code == HTTPStatus.NOT_FOUND
        monkeypatch.setenv("SIGNALWATCH_FEEDBACK_ORIGIN", origin)
        assert client.get(path, params=team).json() is None
        assert client.post(path, json=team).status_code == HTTPStatus.FORBIDDEN
        initial = client.post(path, json=team, headers=headers)
        assert initial.status_code == HTTPStatus.CONFLICT and "saved evidence" in initial.json()["detail"]
        for article in articles:
            assert storage.save_article_evidence(source_id, article.article_url, "Evidence") == "Evidence"
        assert storage.save_article_evidence(source_id, articles[0].article_url, "Refetched") == "Evidence"
        storage.save_feedback(contested.model_copy(update={"id": uuid4(), "judgment": "useful"}))
        conflict = client.post(path, json=team, headers=headers)
        assert conflict.status_code == HTTPStatus.CONFLICT and "disagrees" in conflict.json()["detail"]
        older_miss = contested.model_copy(update={"id": uuid4(), "judgment": "missed", "url": articles[2].article_url})
        storage.save_feedback(older_miss)
        miss = older_miss.model_copy(update={"id": uuid4(), "url": articles[0].article_url})
        storage.save_feedback(miss)

        queued = client.post(path, json=team, headers=headers).json()
        assert queued["status"] == "queued"
        assert client.get(path, params=team).json() == queued
        assert client.post(path, json=team, headers=headers).json() == queued
        assert [(row["title"], row["expected"]) for row in queued["cases"]] == [
            ("miss", True),
            ("shown", None),
            ("held", True),
        ]
        suggestion_id = queued["id"]
        apply_path = f"{path}/{suggestion_id}/apply"
        assert client.post(apply_path, headers=headers).status_code == HTTPStatus.CONFLICT
        cases = [storage.SuggestionCase(**(case | {"current": [], "proposed": []})) for case in queued["cases"]]
        storage.finish_suggestion(suggestion_id, "API changes", cases[:-1], None)
        assert client.get(path, params=team).json()["status"] == "failed"
        assert client.post(apply_path, headers=headers).status_code == HTTPStatus.CONFLICT

        replacement = client.post(path, json=team, headers=headers).json()
        assert replacement["id"] != suggestion_id
        assert replacement["cases"][0]["title"] == "miss"
        assert client.delete(f"{path}/{suggestion_id}", headers=headers).status_code == HTTPStatus.NO_CONTENT
        suggestion_id = replacement["id"]
        apply_path = f"{path}/{suggestion_id}/apply"
        storage.finish_suggestion(suggestion_id, "API changes", cases, None)
        storage.save_team_interests(interests.model_copy(update={"marketing": "New positioning"}))
        assert client.post(apply_path, headers=headers).status_code == HTTPStatus.CONFLICT
        storage.save_team_interests(interests)
        storage.save_analysis_quality(AnalysisQuality.HIGHER_QUALITY)
        assert client.post(apply_path, headers=headers).status_code == HTTPStatus.CONFLICT
        storage.save_analysis_quality(AnalysisQuality.ECONOMY)
        assert client.post(apply_path).status_code == HTTPStatus.FORBIDDEN
        applied = client.post(apply_path, headers=headers).json()
        assert applied["status"] == "applied"
        assert client.post(apply_path, headers=headers).json() == applied
        assert storage.get_team_interests() == interests.model_copy(update={"product": "API changes"})
        assert client.delete(f"{path}/{suggestion_id}", headers=headers).status_code == HTTPStatus.NO_CONTENT
        assert client.get(path, params=team).json() is None
        assert client.post(apply_path, headers=headers).status_code == HTTPStatus.NOT_FOUND

        next_suggestion = client.post(path, json=team, headers=headers).json()
        assert next_suggestion["cases"][0]["title"] == "held"
        assert any(case["title"] == "miss" and case["expected"] for case in next_suggestion["cases"])
        assert client.delete(f"{path}/{next_suggestion['id']}", headers=headers).status_code == HTTPStatus.NO_CONTENT
        after_dismiss = client.post(path, json=team, headers=headers).json()
        assert after_dismiss["cases"][0]["title"] == "held"
        assert client.delete(f"{path}/{after_dismiss['id']}", headers=headers).status_code == HTTPStatus.NO_CONTENT
        storage.save_feedback(miss.model_copy(update={"id": uuid4()}))
        assert client.post(path, json=team, headers=headers).json()["cases"][0]["title"] == "miss"

        patched = client.patch("/api/team-interests", json={"marketing": ""})
        assert patched.status_code == HTTPStatus.OK
        assert patched.json() == interests.model_copy(update={"marketing": "", "product": "API changes"}).model_dump()


def test_digest_returns_recent_edits_across_linked_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    homepage = storage.save_competitor("First", "https://first.example/").pages[0]
    first = storage.save_page(homepage.competitor_id, "https://first.example/updates")
    second = storage.save_competitor("Second", "https://second.example/").pages[0]
    unlinked = storage.save_competitor("Unlinked", "https://unlinked.example/").pages[0]
    for page in (first, second, unlinked, homepage):
        storage.link_watch(page.id, f"watch-{page.id}")
    for page, version, before in (
        (first, 9, "old snapshot"),
        (second, 10, "old snapshot"),
        (first, 10, "old snapshot"),
        (second, 8, "old snapshot"),
        (first, 99, None),
        (unlinked, 100, "old snapshot"),
        (homepage, 11, "old snapshot"),
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
    homepage_analysis = ChangeAnalysisResult(
        summary="Homepage pricing changed.", marketing=marketing, product=product, research_and_development=research
    )
    storage.save_change_analysis(
        first.id,
        9,
        ChangeAnalysisResult(
            summary="Formatting changed.", marketing=product, product=product, research_and_development=product
        ),
    )
    storage.save_team_interests(TeamInterests(product="New feature announcements"))
    storage.set_article_feed(source_id=first.competitor_id, url="https://first.example/feed.xml")
    assert (homepage.id, 11) in storage.list_unqueued_changes()
    assert storage.get_change_for_analysis(homepage.id, 11) == storage.ChangeToAnalyze("old snapshot", "new snapshot")
    storage.save_change_analysis(homepage.id, 11, homepage_analysis)

    with TestClient(app) as client:
        response = client.get("/api/digest")
        held = client.get("/api/digest", params={"view": "held"})
        homepage_changes = client.get(f"/api/pages/{homepage.id}/changes")
        history = client.get(f"/api/pages/{homepage.id}/analysis")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {
            "page_id": homepage.id,
            "version": "11",
            "source_name": "First",
            "page_url": homepage.url,
            "status": "completed",
            "summary": "Homepage pricing changed.",
            "relevant_teams": ["marketing", "research_and_development"],
            "team_reasons": {"marketing": "Pricing changed", "research_and_development": "API terms changed"},
        },
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

    assert homepage_changes.status_code == HTTPStatus.OK
    assert "X-SignalWatch-Hold-Reason" not in homepage_changes.headers
    assert homepage_changes.json() == [{"version": "11", "before": "old snapshot", "after": "new snapshot"}]
    assert history.json()["11"] == {"status": "completed", "result": homepage_analysis.model_dump()}


def test_article_feed_settings_keep_history_and_seed_new_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    source = storage.save_competitor("Releases", "https://releases.example/")
    settings_url = f"{_ARTICLE_FEEDS}/{source.id}"
    first_feed = storage.ArticleFeed(source.id, "https://releases.example/feed.xml")
    next_feed = storage.ArticleFeed(source.id, "https://releases.example/new-feed.xml")
    entry = {"source_id": source.id, "source_name": source.name, "website": source.website}
    new_article = storage.Article("New release", "https://publisher.example/release")
    next_baseline = storage.Article("Already in the new feed", "https://publisher.example/previous")
    stale = storage.Article("From the previous feed", "https://publisher.example/stale")

    with TestClient(app) as client:
        disabled = {**entry, "url": None}
        assert client.get(_ARTICLE_FEEDS).json() == [disabled]
        response = client.put(settings_url, json={"url": "http://127.0.0.1/rss"})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        response = client.put(f"{_ARTICLE_FEEDS}/{source.id + 1}", json={"url": None})
        assert response.status_code == HTTPStatus.NOT_FOUND
        enabled = {"url": first_feed.url}
        response = client.put(settings_url, json=enabled)
        assert response.status_code == HTTPStatus.OK
        assert response.json() == entry | enabled
        assert client.get(_ARTICLE_FEEDS).json() == [entry | enabled]
        assert storage.list_article_feeds() == [first_feed]

        storage.save_discovered_articles(first_feed, [])
        assert client.get("/api/digest", params={"view": "held"}).json() == []
        storage.save_discovered_articles(first_feed, [new_article])

        assert client.put(settings_url, json={"url": next_feed.url}).status_code == HTTPStatus.OK
        storage.save_discovered_articles(next_feed, [next_baseline])
        storage.save_discovered_articles(first_feed, [stale])
        assert client.put(settings_url, json={"url": None}).json() == disabled
        storage.save_discovered_articles(next_feed, [stale])
        assert storage.list_article_feeds() == []
        assert storage.list_unqueued_articles() == [(source.id, new_article.article_url)]
        held = client.get("/api/digest", params={"view": "held"}).json()
        assert [(item["article_url"], item["status"]) for item in held] == [(new_article.article_url, "pending")]


def test_digest_keeps_per_source_articles_and_held_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    monkeypatch.setattr(storage, "time", lambda: 10)
    first = storage.save_competitor("First feed", "https://first.example/")
    second = storage.save_competitor("Second feed", "https://second.example/")
    baseline = storage.Article("Baseline", "https://publisher.example/first")
    article = storage.Article("A new release", "https://publisher.example/release?ref=feed")
    for source in (first, second):
        feed = storage.ArticleFeed(source.id, f"{source.website}feed.xml")
        storage.set_article_feed(source_id=feed.source_id, url=feed.url)
        storage.save_discovered_articles(feed, [baseline])
        storage.save_discovered_articles(feed, [baseline, article, article])
    assert storage.list_unqueued_articles() == [(first.id, article.article_url), (second.id, article.article_url)]

    held = TeamDecision(relevant=False, reason="No match", quote=None)
    product = TeamDecision(relevant=True, reason="Review the rollout options", quote="Publisher evidence")
    result = ArticleAnalysisResult(
        summary=article.title,
        marketing=held,
        product=product,
        research_and_development=held,
        change_quote="Publisher evidence",
    )
    storage.save_article_analysis(first.id, article.article_url, result)
    legacy = {**result.model_dump(exclude={"change_quote"}), "business_quote": result.change_quote}
    with sqlite3.connect(db_path) as database:
        database.execute("""ALTER TABLE article_feeds ADD COLUMN
            discovery_only INTEGER NOT NULL DEFAULT 1 CHECK (discovery_only IN (0, 1))""")
        database.execute(
            "INSERT INTO discovered_article_analyses VALUES (?, ?, 'completed', ?)",
            (second.id, article.article_url, json.dumps(legacy)),
        )
    assert storage.list_article_feeds() == [
        storage.ArticleFeed(source.id, f"{source.website}feed.xml") for source in (first, second)
    ]
    with sqlite3.connect(db_path) as database:
        columns = [row[1] for row in database.execute("PRAGMA table_info(article_feeds)")]
        assert columns == ["source_id", "url", "seeded"]
    unmatched = storage.Article("An operations update", "https://publisher.example/update")
    storage.save_discovered_articles(storage.ArticleFeed(second.id, f"{second.website}feed.xml"), [unmatched])
    unmatched_result = result.model_copy(update={"product": held, "summary": unmatched.title})
    storage.save_article_analysis(second.id, unmatched.article_url, unmatched_result)
    expected = {
        "kind": "article",
        "title": article.title,
        "article_url": article.article_url,
        "version": "10",
        "status": "completed",
        "summary": article.title,
    }
    held_article = {
        **expected,
        "source_id": second.id,
        "source_name": second.name,
        "relevant_teams": [],
        "team_reasons": {"marketing": held.reason, "product": held.reason, "research_and_development": held.reason},
    }
    legacy_article = {**held_article, "team_reasons": {}, "hold_reason": "business_fit_unverified"}
    unmatched_article = {
        **held_article,
        "article_url": unmatched.article_url,
        "title": unmatched.title,
        "summary": unmatched.title,
    }

    with TestClient(app) as client:
        assert client.get("/api/digest").json() == []
        storage.save_team_interests(TeamInterests(business_context="Enterprise controls", product="Rollout options"))
        assert client.get("/api/digest").json() == [
            {
                **expected,
                "source_id": first.id,
                "source_name": first.name,
                "relevant_teams": ["product"],
                "team_reasons": {"product": product.reason},
            }
        ]
        assert client.get("/api/digest", params={"view": "held"}).json() == [legacy_article, unmatched_article]
        assert client.delete(f"/api/competitors/{first.id}").status_code == HTTPStatus.NO_CONTENT
        assert client.get("/api/digest").json() == []
        assert client.get("/api/digest", params={"view": "held"}).json() == [legacy_article, unmatched_article]
    assert storage.get_discovered_article(first.id, article.article_url) is None
    assert storage.list_article_feeds() == [storage.ArticleFeed(second.id, f"{second.website}feed.xml")]


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
        if path == f"/api/v1/watch/{_WATCH_ID}":
            return httpx2.Response(
                HTTPStatus.OK, json={"paused": False, "last_checked": 0, "check_count": 0, "last_error": False}
            )
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


def test_monitoring_tracks_successful_watches_and_feeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.delenv("SIGNALWATCH_PAGE_STALE_SECONDS", raising=False)
    now = 20_000
    monkeypatch.setattr(storage, "time", lambda: now)
    source = storage.save_competitor("Example", "https://example.com/")
    page = source.pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    feed_url = "https://example.com/feed.xml"
    storage.set_article_feed(source_id=source.id, url=feed_url)

    watch = changedetection.WatchStatus(available=True, check_count=3, last_checked=now)
    monkeypatch.setattr(changedetection, "read_watch", lambda _watch_id: watch)
    monkeypatch.setattr(changedetection, "read_history", lambda _watch_id: [])
    empty_feed = b'<rss version="2.0"><channel></channel></rss>'
    feed_body = empty_feed
    monkeypatch.setattr(
        article_feeds, "read_public_content", lambda _url: PublicContent(feed_body, "text/xml", "utf-8")
    )

    with TestClient(app) as client:
        health = "/api/monitoring-health"
        detail = "/api/maintainer/monitoring"
        assert client.get(health).status_code == HTTPStatus.OK
        asyncio.run(_catch_up_once(Event()))

        now += 10
        watch = watch.model_copy(update={"check_count": 4, "last_checked": now})
        asyncio.run(_catch_up_once(Event()))
        article_feeds.sync_feeds()
        last_success = now

        now += 20 * 60
        feed_body = b"<html></html>"
        article_feeds.sync_feeds()
        stale_feed = {
            "id": source.id,
            "kind": "feed",
            "url": feed_url,
            "source_name": source.name,
            "last_success_at": last_success,
        }
        response = client.get(health)
        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.content == b"" and response.headers["Cache-Control"] == "no-store"
        assert client.get(detail).json() == [stale_feed]

        now += 4 * 60 * 60
        watch = watch.model_copy(update={"check_count": 5, "last_checked": now, "error": "Last page fetch failed."})
        asyncio.run(_catch_up_once(Event()))
        # A failed completed check and a new attempt with the same counter are both stale.
        watch = watch.model_copy(update={"last_checked": now + 0.5, "error": None})
        asyncio.run(_catch_up_once(Event()))
        stale_page = {
            "id": page.id,
            "kind": "page",
            "url": page.url,
            "source_name": source.name,
            "last_success_at": last_success,
        }
        assert client.get(detail).json() == [stale_feed, stale_page]

        now += 1
        watch = watch.model_copy(update={"check_count": 6, "last_checked": now})
        asyncio.run(_catch_up_once(Event()))
        feed_body = empty_feed
        article_feeds.sync_feeds()
        assert client.get(health).status_code == HTTPStatus.OK
        response = client.get(detail)
        assert response.headers["Cache-Control"] == "no-store" and response.json() == []


def test_monitoring_grace_for_existing_and_paused_watches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    monkeypatch.delenv("SIGNALWATCH_PAGE_STALE_SECONDS", raising=False)
    now = 20_000
    monkeypatch.setattr(storage, "time", lambda: now)
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    with closing(sqlite3.connect(db_path)) as database:
        database.executescript("DROP TABLE page_monitoring; DROP TABLE feed_monitoring;")

    assert storage.list_stale_sources() == []
    now += 4 * 60 * 60
    assert [source.last_success_at for source in storage.list_stale_sources()] == [None]
    watch = storage.LinkedWatch(page.id, _WATCH_ID)
    storage.record_page_check(watch, 4, None, True)
    assert storage.list_stale_sources() == []
    now += 4 * 60 * 60
    assert [source.id for source in storage.list_stale_sources()] == [page.id]
    storage.record_page_check(watch, 4, None, False)
    assert storage.list_stale_sources() == []


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


def test_legacy_migrations_preserve_saved_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "configuration.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    storage.save_page_version(page_id=page.id, watch_id=_WATCH_ID, version=10, before="Before", after="After")
    storage.record_history_gap(page.id, _WATCH_ID)
    held = TeamDecision(relevant=False, reason="No match", quote=None)
    analysis = ChangeAnalysisResult(summary="A change", marketing=held, product=held, research_and_development=held)
    storage.save_change_analysis(page.id, 10, analysis)

    with sqlite3.connect(db_path) as database:
        legacy_pages_schema = storage._PAGES_SCHEMA.replace(" AUTOINCREMENT", "")
        database.executescript(f"""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE legacy_pages {legacy_pages_schema};
            INSERT INTO legacy_pages SELECT * FROM pages;
            DROP TABLE pages;
            ALTER TABLE legacy_pages RENAME TO pages;
            CREATE TABLE legacy_page_changes (
                page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                version TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, PRIMARY KEY (page_id, version)
            );
            INSERT INTO legacy_page_changes SELECT * FROM page_changes;
            DROP TABLE page_changes;
            ALTER TABLE legacy_page_changes RENAME TO page_changes;
        """)

    with TestClient(app) as client:
        changes = client.get(f"/api/pages/{page.id}/changes")
        assert changes.json() == [{"version": "10", "before": "Before", "after": "After"}]
        assert changes.headers[_HISTORY_GAP_HEADER] == "true"
        assert client.get(f"/api/pages/{page.id}/analysis").json() == {
            "10": {"status": "completed", "result": analysis.model_dump()}
        }

    with sqlite3.connect(db_path) as database:
        assert database.execute("SELECT typeof(version) FROM page_changes").fetchone() == ("integer",)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


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
