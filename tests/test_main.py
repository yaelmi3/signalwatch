import json
import sqlite3
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
from fastapi.testclient import TestClient

from signalwatch import storage
from signalwatch.main import app


def test_serves_frontend_and_api_docs() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == HTTPStatus.OK
        assert page.headers["content-type"].startswith("text/html")
        assert "<title>SignalWatch</title>" in page.text

        docs = client.get("/docs")
        assert docs.status_code == HTTPStatus.OK
        assert "Swagger UI" in docs.text


def test_configuration_persists_and_links_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(tmp_path / "configuration.sqlite3"))
    monkeypatch.setenv("CHANGEDETECTION_URL", "http://engine.test")
    monkeypatch.setenv("CHANGEDETECTION_API_KEY", "test-api-key")
    watch_id = "520e831c-6d74-4c6d-96c6-c782061b0249"
    extra_watch_id = "a655d37b-0928-4a73-aa9f-031411c407bc"
    handler = Mock(
        side_effect=[
            httpx2.Response(HTTPStatus.CREATED, json={"uuid": watch_id}),
            httpx2.Response(HTTPStatus.OK, json={"url": "https://example.com/"}),
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
            httpx2.Response(HTTPStatus.OK, json={"url": "https://example.com/pricing"}),
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
        "notification_muted": True,
    }
    assert json.loads(handler.call_args_list[4].args[0].content) == {
        "url": "https://example.com/pricing",
        "title": "Example",
        "fetch_backend": "html_requests",
        "notification_muted": True,
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
