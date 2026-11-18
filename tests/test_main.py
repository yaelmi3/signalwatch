import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import httpx2
import pytest
from fastapi.testclient import TestClient

from signalwatch import storage
from signalwatch.main import app

_WEBHOOK = "/api/webhooks/changedetection"
_WEBHOOK_HEADERS = {"X-SignalWatch-Webhook-Secret": "test-webhook-secret"}
_WEBHOOK_SETUP_URL = "https://hooks.example.com/api/webhooks/changedetection"
_WEBHOOK_SETUP_SECRET = "test-webhook+secret"
_WATCH_NOTIFICATIONS = {
    "notification_muted": False,
    "notification_urls": [
        "jsons://hooks.example.com/api/webhooks/changedetection?%2BX-SignalWatch-Webhook-Secret=test-webhook%2Bsecret"
    ],
    "notification_format": "text",
    "notification_body": "{{watch_uuid}}",
}
_WATCH_ID = "520e831c-6d74-4c6d-96c6-c782061b0249"


def test_serves_frontend_and_api_docs() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == HTTPStatus.OK
        assert page.headers["content-type"].startswith("text/html")
        assert "<title>SignalWatch</title>" in page.text

        docs = client.get("/docs")
        assert docs.status_code == HTTPStatus.OK
        assert "Swagger UI" in docs.text


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
    storage_race = Barrier(2, timeout=5)

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["x-api-key"] == "test-api-key"
        if request.method == HTTPMethod.DELETE and request.url.path == engine_path:
            return httpx2.Response(HTTPStatus.NO_CONTENT)
        assert request.method == HTTPMethod.GET
        match request.url.path:
            case path if path == f"{engine_path}/history":
                return httpx2.Response(HTTPStatus.OK, json=history)
            case path if path == f"{engine_path}/history/10":
                return httpx2.Response(HTTPStatus.OK, text="Price: $10")
            case path if path == f"{engine_path}/history/11":
                storage_race.wait()
                return httpx2.Response(HTTPStatus.OK, text="Price: $15")
        raise AssertionError(f"Unexpected snapshot request: {request.url.path}")

    monkeypatch.setattr(httpx2, "Client", partial(httpx2.Client, transport=httpx2.MockTransport(handle)))
    payload = {"title": "Watch changed", "message": _WATCH_ID, "version": "1.1"}
    changes_url = f"/api/pages/{page.id}/changes"
    with TestClient(app) as client:
        baseline = client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS)
        assert baseline.status_code == HTTPStatus.NO_CONTENT
        assert client.get(changes_url).json() == []

        history["11"] = "latest.txt"
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(client.post, _WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS) for _ in range(2)]
            assert [call.result().status_code for call in calls] == [HTTPStatus.NO_CONTENT, HTTPStatus.NO_CONTENT]
        assert client.get(changes_url).json() == [{"version": "11", "before": "Price: $10", "after": "Price: $15"}]

        assert client.delete(f"/api/pages/{page.id}").status_code == HTTPStatus.NO_CONTENT
        assert client.get(changes_url).status_code == HTTPStatus.NOT_FOUND
    with sqlite3.connect(db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM page_changes").fetchone() == (0,)


def test_changedetection_webhook_catches_up_in_order_and_resumes_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    monkeypatch.setenv("CHANGEDETECTION_WEBHOOK_SECRET", "test-webhook-secret")
    page = storage.save_competitor("Example", "https://example.com/").pages[0]
    storage.link_watch(page.id, _WATCH_ID)
    retained_history = {"11": "e.txt", "10": "d.txt", "3": "c.txt"}
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.OK, json={"2": "b.txt", "1": "a.txt"}),
            httpx2.Response(HTTPStatus.OK, text="A"),
            httpx2.Response(HTTPStatus.OK, text="B"),
            httpx2.Response(HTTPStatus.OK, json={"10": "d.txt", "3": "c.txt"}),
            httpx2.Response(HTTPStatus.OK, text="C"),
            httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE),
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
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.BAD_GATEWAY
        completed = [{"version": "3", "before": "B", "after": "C"}, {"version": "2", "before": "A", "after": "B"}]
        assert client.get(changes_url).json() == completed

        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        expected = [{"version": "11", "before": "D", "after": "E"}, {"version": "10", "before": "C", "after": "D"}]
        assert client.get(changes_url).json() == expected + completed
        assert client.post(_WEBHOOK, json=payload, headers=_WEBHOOK_HEADERS).status_code == HTTPStatus.NO_CONTENT
        assert client.get(changes_url).json() == expected + completed

    history_path = f"/api/v1/watch/{_WATCH_ID}/history"
    assert [call.args[0].url.path.removeprefix(history_path) for call in handler.call_args_list] == [
        "",
        "/1",
        "/2",
        "",
        "/3",
        "/10",
        "",
        "/10",
        "/11",
        "",
    ]


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
