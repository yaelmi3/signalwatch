import json
from base64 import b64decode
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from signalwatch import slack_delivery, storage
from signalwatch.analysis_models import ChangeAnalysisResult, TeamDecision
from signalwatch.main import app

_ROOT = "/api/maintainer/slack"
_ORIGIN = "https://signalwatch.test"


def test_slack_operator_connects_selects_tests_and_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "slack.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    key = Fernet.generate_key()
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_ID", "123.456")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("SIGNALWATCH_SLACK_REDIRECT_URL", f"{_ORIGIN}{_ROOT}/callback")
    monkeypatch.setenv("SIGNALWATCH_SLACK_MAINTAINER_PASSWORD", "fake-passphrase")
    monkeypatch.setenv("SIGNALWATCH_SLACK_ENCRYPTION_KEY", key.decode())
    monkeypatch.setenv("SIGNALWATCH_SLACK_BOT_TOKEN", "fake-legacy-token")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CHANNEL_ID", "C_OLD")
    requests: list[httpx2.Request] = []

    def slack_response(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "slack.com"
        requests.append(request)
        if request.url.path == "/api/oauth.v2.access":
            assert request.method == HTTPMethod.POST
            assert b64decode(request.headers["authorization"].removeprefix("Basic ")) == b"123.456:fake-client-secret"
            assert parse_qs(request.content.decode()) == {
                "code": ["fake-install-code"],
                "redirect_uri": [f"{_ORIGIN}{_ROOT}/callback"],
            }
            body = {"token_type": "bot", "access_token": "fake-bot-token", "team": {"name": "Sandbox"}}
        else:
            assert request.headers["authorization"] == "Bearer fake-bot-token"
            if request.url.path == "/api/users.conversations":
                assert request.method == HTTPMethod.GET
                params = request.url.params
                assert params["types"] == "public_channel,private_channel" and params["exclude_archived"] == "true"
                assert "user" not in params
                channels = [{"id": "G_TEAM", "name": "team-private"}]
                if not params.get("cursor"):
                    channels = [
                        {"id": "C_TEAM", "name": "team-public"},
                        {"id": "C_ARCHIVED", "name": "archived", "is_archived": True},
                    ]
                body = {
                    "channels": channels,
                    "response_metadata": {"next_cursor": "" if params.get("cursor") else "next"},
                }
            else:
                assert request.url.path == "/api/chat.postMessage" and request.method == HTTPMethod.POST
                body_data = json.loads(request.content)
                assert body_data["channel"] == "G_TEAM"
                assert body_data["text"] == "SignalWatch test message. Slack is connected."
                assert not body_data["mrkdwn"] and not body_data["unfurl_links"]
                body = {}
        return httpx2.Response(HTTPStatus.OK, json={"ok": True, **body})

    class FakeClient(httpx2.Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(transport=httpx2.MockTransport(slack_response), **kwargs)

    monkeypatch.setattr(httpx2, "Client", FakeClient)
    public = {
        "available": True,
        "authenticated": False,
        "connected": False,
        "workspace_name": None,
        "channel_id": None,
        "enabled": False,
    }
    with TestClient(app, base_url=_ORIGIN) as operator, TestClient(app, base_url=_ORIGIN) as visitor:
        assert visitor.get(_ROOT).json() == public
        assert visitor.get(f"{_ROOT}/channels").status_code == HTTPStatus.UNAUTHORIZED
        assert visitor.post(f"{_ROOT}/connect", json={}).status_code == HTTPStatus.UNAUTHORIZED
        assert slack_delivery.get_settings() is None
        password = {"password": "fake-passphrase"}
        assert operator.post(f"{_ROOT}/session", json=password).status_code == HTTPStatus.FORBIDDEN
        operator.headers["Origin"] = _ORIGIN
        invalid = operator.post(f"{_ROOT}/session", json={"password": ["never-echo-this"]})
        assert invalid.status_code == HTTPStatus.UNAUTHORIZED and "never-echo-this" not in invalid.text
        session = operator.post(f"{_ROOT}/session", json=password)
        assert session.status_code == HTTPStatus.NO_CONTENT and not session.content
        cookie = session.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", "max-age="))
        assert operator.get(_ROOT).json() == {**public, "authenticated": True}
        auth_url = urlsplit(operator.post(f"{_ROOT}/connect", json={}).json()["url"])
        params = parse_qs(auth_url.query)
        assert auth_url.netloc == "slack.com" and auth_url.path == "/oauth/v2/authorize"
        assert params["scope"] == ["chat:write,channels:read,groups:read"] and "user_scope" not in params
        callback = f"{_ROOT}/callback"
        query = {"code": "fake-install-code", "state": params["state"][0]}
        forged = operator.get(callback, params={**query, "state": "other"}, follow_redirects=False)
        foreign = visitor.get(callback, params=query, follow_redirects=False)
        assert forged.headers["location"] == foreign.headers["location"] == "/?view=maintainer&slack=error"
        assert not requests
        returned = operator.get(callback, params=query, follow_redirects=False)
        assert returned.status_code == HTTPStatus.SEE_OTHER
        assert returned.headers["location"] == "/?view=maintainer&slack=connected"
        assert "fake-install-code" not in returned.text + str(returned.headers)
        replay = operator.get(callback, params=query, follow_redirects=False)
        assert replay.headers["location"] == "/?view=maintainer&slack=error" and len(requests) == 1
        assert visitor.get(_ROOT).json() == public
        connected = {**public, "authenticated": True, "connected": True, "workspace_name": "Sandbox"}
        assert operator.get(_ROOT).json() == connected
        connection = storage.get_slack_connection()
        assert connection and Fernet(key).decrypt(connection.encrypted_token) == b"bot:fake-bot-token"
        assert b"fake-bot-token" not in db_path.read_bytes()
        assert operator.get(f"{_ROOT}/channels").json() == [
            {"id": "G_TEAM", "name": "team-private"},
            {"id": "C_TEAM", "name": "team-public"},
        ]
        destination = {"channel_id": "G_TEAM", "enabled": False}
        cross_site = operator.put(_ROOT, json=destination, headers={"Origin": "https://other.test"})
        assert cross_site.status_code == HTTPStatus.FORBIDDEN
        assert operator.put(_ROOT, json={**destination, "channel_id": "C_OTHER"}).status_code == HTTPStatus.BAD_REQUEST
        assert operator.put(_ROOT, json=destination).json() == {**connected, **destination}
        assert slack_delivery.configured_channel() is None
        test = operator.post(f"{_ROOT}/test", json={})
        assert test.status_code == HTTPStatus.NO_CONTENT and not test.content
        assert operator.get(_ROOT).json()["enabled"] is False
        assert operator.put(_ROOT, json={**destination, "enabled": True}).json()["enabled"] is True
        assert slack_delivery.configured_channel() == "G_TEAM"
        previously_enabled = slack_delivery.get_settings()
        assert previously_enabled is not None and previously_enabled.bot_token == "fake-bot-token"
        page = storage.save_competitor("Test source", "https://publisher.test/").pages[0]
        storage.link_watch(page.id, "fake-watch")
        assert storage.save_page_version(
            page_id=page.id, watch_id="fake-watch", version=1, before="Previous", after="Update"
        )
        held = TeamDecision(relevant=False, reason="No match.", quote=None)
        analysis = ChangeAnalysisResult(
            summary="Update",
            marketing=held,
            research_and_development=held,
            product=TeamDecision(relevant=True, reason="Test match.", quote="Update"),
        )
        storage.save_change_analysis(page.id, 1, analysis, slack_channel=slack_delivery.configured_channel())
        assert operator.put(_ROOT, json=destination).json()["enabled"] is False
        assert slack_delivery.get_settings() is None
        slack_delivery.send_next(previously_enabled)
        assert sum(request.url.path == "/api/chat.postMessage" for request in requests) == 1


def test_incomplete_oauth_is_unavailable_and_legacy_worker_mode_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGNALWATCH_SLACK_BOT_TOKEN", "fake-legacy-token")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CHANNEL_ID", "C_OLD")
    assert slack_delivery.configured_channel() == "C_OLD"
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_ID", "123.456")
    assert not TestClient(app, base_url=_ORIGIN).get(_ROOT).json()["available"]
    assert slack_delivery.get_settings() is None
