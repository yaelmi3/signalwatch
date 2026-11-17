import json
from functools import partial
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
from fastapi.testclient import TestClient

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
